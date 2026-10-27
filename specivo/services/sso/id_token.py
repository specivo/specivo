"""Validation of ID tokens and back-channel logout tokens.

ID tokens: OpenID Connect Core 1.0, section 3.1.3.7 (ID token validation),
3.1.3.8 (``at_hash``) and 15.5.2 (nonce). Logout tokens: OpenID Connect
Back-Channel Logout 1.0, section 2.6 (logout token validation). Both share
one decoder:

1. the JOSE header ``alg`` must be in the allowlist (RS/PS/ES/EdDSA ∩ the
   provider's discovery list; never ``none`` or HS*), and the key is chosen by
   ``kid`` from the provider's JWKS (:mod:`specivo.services.sso.jwks`);
2. PyJWT verifies the signature with that key bound to that algorithm, RSA
   keys shorter than 2048 bits are refused, ``iss`` must equal the configured
   issuer exactly, ``aud`` must contain the client id, and required claims
   must be present;
3. time claims are checked here rather than by PyJWT, so callers and tests
   can pass ``now``: ``exp`` not passed, ``iat`` not in the future and not
   older than :data:`MAX_TOKEN_AGE_SECONDS`, ``nbf`` reached, all with the
   ``SSO_CLOCK_SKEW_SECONDS`` leeway;
4. token-specific rules: ``azp``, ``nonce`` (constant-time), ``at_hash`` and,
   when the request sent ``max_age``, a present and fresh ``auth_time`` for
   ID tokens; ``events``, ``jti``, ``sid``/``sub``, no ``nonce`` and
   ``typ=logout+jwt`` for logout tokens.

Every rejection raises :class:`SsoTokenInvalidError` (``sso_token_invalid``)
with a :class:`TokenRejection` reason; :class:`SsoUnknownSigningKeyError`
marks an unknown ``kid``. The synchronous ``validate_*`` functions are pure;
``verify_*`` add the JWKS lookup and the one rate-limited refetch on an
unknown ``kid`` (key rotation). The jti replay cache for logout tokens
belongs to the back-channel logout handler.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import jwt
from jwt.exceptions import (
    InvalidAlgorithmError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidKeyError,
    InvalidSignatureError,
    MissingRequiredClaimError,
    PyJWTError,
)
from redis.asyncio import Redis

from specivo.core.config import SSO_MAX_CLOCK_SKEW_SECONDS, Settings, get_settings
from specivo.models.sso import SsoProvider
from specivo.services.sso.discovery import ProviderMetadata
from specivo.services.sso.errors import (
    SsoSigningKeyRefreshLimitedError,
    SsoTokenInvalidError,
    SsoUnknownSigningKeyError,
    TokenRejection,
)
from specivo.services.sso.jwks import (
    get_jwks,
    refetch_jwks_for_unknown_kid,
    select_signing_key,
    signing_algorithms,
)
from specivo.services.sso.log import logger, sanitize_idp_text

__all__ = [
    "BACKCHANNEL_LOGOUT_EVENT",
    "MAX_TOKEN_AGE_SECONDS",
    "IdTokenClaims",
    "LogoutTokenClaims",
    "compute_at_hash",
    "validate_id_token",
    "validate_logout_token",
    "verify_id_token",
    "verify_logout_token",
]

#: A token whose ``iat`` is older than this (plus leeway) is refused.
MAX_TOKEN_AGE_SECONDS = 600

#: Back-Channel Logout 1.0, section 2.4: the member of the ``events`` claim.
BACKCHANNEL_LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"

#: Longest ``sub`` accepted (``user_identities.subject`` is String(255)).
MAX_SUBJECT_LENGTH = 255

_ID_TOKEN_REQUIRED = ("iss", "sub", "aud", "exp", "iat")
_LOGOUT_TOKEN_REQUIRED = ("iss", "aud", "iat", "exp", "jti")
_LOGOUT_TYPES = frozenset({"logout+jwt", "application/logout+jwt"})
# Upper bound for numeric dates (year 5138); larger values are malformed.
_MAX_NUMERIC_DATE = 100_000_000_000


@dataclass(frozen=True)
class IdTokenClaims:
    """A validated ID token. ``claims`` holds every claim; it is kept out of ``repr``."""

    issuer: str
    subject: str
    audience: tuple[str, ...]
    expires_at: int
    issued_at: int
    nonce: str
    azp: str | None = None
    sid: str | None = None
    auth_time: int | None = None
    claims: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class LogoutTokenClaims:
    """A validated back-channel logout token. At least one of ``subject``/``sid`` is set."""

    issuer: str
    audience: tuple[str, ...]
    issued_at: int
    expires_at: int
    jti: str
    subject: str | None = None
    sid: str | None = None
    claims: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reject(reason: TokenRejection, detail: str | None = None) -> SsoTokenInvalidError:
    return SsoTokenInvalidError(reason, detail=detail)


def compute_at_hash(access_token: str, alg: str) -> str:
    """``at_hash`` for ``access_token`` (Core 1.0, section 3.1.3.6).

    The left half of the hash named by the signing algorithm, base64url.
    EdDSA uses SHA-512 (Ed25519, the only EdDSA curve IdPs use in practice).
    """
    digest = {"256": hashlib.sha256, "384": hashlib.sha384}.get(alg[-3:], hashlib.sha512)
    full = digest(access_token.encode("ascii")).digest()
    return base64.urlsafe_b64encode(full[: len(full) // 2]).rstrip(b"=").decode("ascii")


def _resolve_time(now: float | None, leeway: float | None) -> tuple[float, float]:
    current = time.time() if now is None else now
    skew = get_settings().sso_clock_skew_seconds if leeway is None else leeway
    return current, max(0.0, min(float(skew), float(SSO_MAX_CLOCK_SKEW_SECONDS)))


def _numeric_date(payload: Mapping[str, Any], name: str) -> int | None:
    value = payload.get(name)
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0 <= value < _MAX_NUMERIC_DATE
    ):
        raise _reject(TokenRejection.MALFORMED, name)
    return int(value)


def _required_date(payload: Mapping[str, Any], name: str) -> int:
    value = _numeric_date(payload, name)
    if value is None:  # pragma: no cover - PyJWT's "require" option rejects it first
        raise _reject(TokenRejection.MISSING_CLAIM, name)
    return value


def _check_times(payload: Mapping[str, Any], now: float, leeway: float) -> tuple[int, int]:
    exp = _required_date(payload, "exp")
    iat = _required_date(payload, "iat")
    nbf = _numeric_date(payload, "nbf")
    if exp <= now - leeway:
        raise _reject(TokenRejection.EXPIRED)
    if iat > now + leeway:
        raise _reject(TokenRejection.ISSUED_IN_FUTURE)
    if iat < now - MAX_TOKEN_AGE_SECONDS - leeway:
        raise _reject(TokenRejection.TOO_OLD)
    if nbf is not None and nbf > now + leeway:
        raise _reject(TokenRejection.NOT_YET_VALID)
    return exp, iat


def _optional_identifier(payload: Mapping[str, Any], name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_SUBJECT_LENGTH:
        raise _reject(TokenRejection.MALFORMED, name)
    return value


def _audiences(payload: Mapping[str, Any]) -> tuple[str, ...]:
    aud = payload["aud"]  # PyJWT has checked it is a string or a list of strings
    return (aud,) if isinstance(aud, str) else tuple(aud)


def _decode(
    raw: Any,
    *,
    issuer: str,
    client_id: str,
    keys: Iterable[Mapping[str, Any]],
    algorithms: Iterable[str],
    required: Sequence[str],
    check_typ: Callable[[Any], None],
) -> dict[str, Any]:
    """Signature, ``iss``, ``aud`` and presence of ``required``; returns the payload."""
    if not isinstance(raw, str) or raw.count(".") != 2:
        raise _reject(TokenRejection.MALFORMED, "not a compact JWS")
    try:
        header = jwt.get_unverified_header(raw)
    except PyJWTError:
        # Also covers a "crit" header naming an extension PyJWT does not
        # understand (RFC 7515, section 4.1.11).
        raise _reject(TokenRejection.MALFORMED, "header") from None
    check_typ(header.get("typ"))

    key = select_signing_key(keys, header, algorithms)
    try:
        payload = jwt.decode(
            raw,
            key=key,
            algorithms=[key.algorithm_name],
            audience=client_id,
            issuer=issuer,
            options={
                "verify_signature": True,
                "verify_aud": True,
                "verify_iss": True,
                "verify_sub": True,
                "verify_jti": True,
                # Time claims are checked by _check_times with an injectable clock.
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
                "require": list(required),
                "strict_aud": False,
                "enforce_minimum_key_length": True,
            },
        )
    except InvalidSignatureError:
        raise _reject(TokenRejection.BAD_SIGNATURE) from None
    except MissingRequiredClaimError as exc:
        raise _reject(TokenRejection.MISSING_CLAIM, str(exc.claim)) from None
    except InvalidIssuerError:
        raise _reject(TokenRejection.WRONG_ISSUER) from None
    except InvalidAudienceError:
        raise _reject(TokenRejection.WRONG_AUDIENCE) from None
    except InvalidKeyError:
        raise _reject(TokenRejection.KEY_MISMATCH, "key rejected by the verifier") from None
    except InvalidAlgorithmError:  # pragma: no cover - select_signing_key binds the algorithm first
        raise _reject(TokenRejection.ALG_NOT_ALLOWED) from None
    except (PyJWTError, ValueError, TypeError, AttributeError):
        raise _reject(TokenRejection.MALFORMED, "payload") from None
    if not isinstance(payload, dict):  # pragma: no cover - PyJWT returns a dict or raises
        raise _reject(TokenRejection.MALFORMED, "payload")
    return payload


def _log_rejection(kind: str, issuer: str, exc: SsoTokenInvalidError) -> None:
    detail = sanitize_idp_text(exc.detail, 80) if exc.detail else "-"
    logger.warning("sso.%s.rejected iss=%s reason=%s detail=%s", kind, issuer, exc.reason, detail)


# ---------------------------------------------------------------------------
# ID token
# ---------------------------------------------------------------------------


def _check_id_token_typ(typ: Any) -> None:
    # ID tokens carry "JWT" or no typ. Any explicitly profiled "+jwt" type
    # (logout+jwt, at+jwt, ...) is a different kind of token.
    if typ is not None and (not isinstance(typ, str) or typ.lower().endswith("+jwt")):
        raise _reject(TokenRejection.WRONG_TYP)


def validate_id_token(
    raw: str,
    *,
    issuer: str,
    client_id: str,
    keys: Iterable[Mapping[str, Any]],
    algorithms: Iterable[str],
    nonce: str,
    access_token: str | None = None,
    now: float | None = None,
    leeway: float | None = None,
    max_age: int | None = None,
) -> IdTokenClaims:
    """Validate an ID token (Core 1.0, section 3.1.3.7) and return its claims.

    ``max_age`` is the value sent in the authorization request, if any: then
    ``auth_time`` is required and must be at most ``max_age`` (plus leeway)
    seconds old (section 3.1.3.7, item 13).

    ``keys`` are the provider's usable JWKs, ``algorithms`` its allowlist
    (:func:`~specivo.services.sso.jwks.signing_algorithms`), ``nonce`` the value
    sent in the authorization request. ``at_hash`` is checked when both the
    claim and ``access_token`` are present. ``leeway`` defaults to
    ``SSO_CLOCK_SKEW_SECONDS``.
    """
    try:
        return _validate_id_token(
            raw,
            issuer=issuer,
            client_id=client_id,
            keys=keys,
            algorithms=algorithms,
            nonce=nonce,
            access_token=access_token,
            now=now,
            leeway=leeway,
            max_age=max_age,
        )
    except SsoTokenInvalidError as exc:
        _log_rejection("id_token", issuer, exc)
        raise


def _validate_id_token(
    raw: str,
    *,
    issuer: str,
    client_id: str,
    keys: Iterable[Mapping[str, Any]],
    algorithms: Iterable[str],
    nonce: str,
    access_token: str | None,
    now: float | None,
    leeway: float | None,
    max_age: int | None,
) -> IdTokenClaims:
    current, skew = _resolve_time(now, leeway)
    payload = _decode(
        raw,
        issuer=issuer,
        client_id=client_id,
        keys=keys,
        algorithms=algorithms,
        required=_ID_TOKEN_REQUIRED,
        check_typ=_check_id_token_typ,
    )
    exp, iat = _check_times(payload, current, skew)
    auth_time = _numeric_date(payload, "auth_time")
    if max_age is not None:
        if auth_time is None:
            raise _reject(TokenRejection.AUTH_TIME_MISSING, "max_age was requested")
        if current - auth_time > max_age + skew:
            raise _reject(TokenRejection.AUTH_TIME_STALE, f"max_age={max_age}")

    subject = payload["sub"]  # PyJWT has checked it is a string
    if not subject:
        raise _reject(TokenRejection.MISSING_CLAIM, "sub")
    if len(subject) > MAX_SUBJECT_LENGTH:
        raise _reject(TokenRejection.MALFORMED, "sub")

    audiences = _audiences(payload)
    azp = payload.get("azp")
    if azp is not None and azp != client_id:
        raise _reject(TokenRejection.WRONG_AZP)
    if len(audiences) > 1 and azp is None:
        raise _reject(TokenRejection.WRONG_AZP, "several audiences and no azp")

    token_nonce = payload.get("nonce")
    if token_nonce is None:
        raise _reject(TokenRejection.MISSING_CLAIM, "nonce")
    if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce.encode("utf-8"), nonce.encode("utf-8")):
        raise _reject(TokenRejection.NONCE_MISMATCH)

    claimed_at_hash = payload.get("at_hash")
    if claimed_at_hash is not None and access_token is not None:
        alg = jwt.get_unverified_header(raw)["alg"]
        if not isinstance(claimed_at_hash, str) or not hmac.compare_digest(
            claimed_at_hash.encode("utf-8"), compute_at_hash(access_token, alg).encode("ascii")
        ):
            raise _reject(TokenRejection.AT_HASH_MISMATCH)

    return IdTokenClaims(
        issuer=payload["iss"],
        subject=subject,
        audience=audiences,
        expires_at=exp,
        issued_at=iat,
        nonce=token_nonce,
        azp=azp,
        sid=payload.get("sid") if isinstance(payload.get("sid"), str) else None,
        auth_time=auth_time,
        claims=payload,
    )


# ---------------------------------------------------------------------------
# Logout token
# ---------------------------------------------------------------------------


def _check_logout_typ(typ: Any) -> None:
    # Back-Channel Logout 1.0, section 2.4: typ is "logout+jwt" when present.
    if typ is not None and (not isinstance(typ, str) or typ.lower() not in _LOGOUT_TYPES):
        raise _reject(TokenRejection.WRONG_TYP)


def validate_logout_token(
    raw: str,
    *,
    issuer: str,
    client_id: str,
    keys: Iterable[Mapping[str, Any]],
    algorithms: Iterable[str],
    now: float | None = None,
    leeway: float | None = None,
) -> LogoutTokenClaims:
    """Validate a back-channel logout token (Back-Channel Logout 1.0, section 2.6).

    The caller still has to reject a replayed ``jti``.
    """
    try:
        return _validate_logout_token(
            raw, issuer=issuer, client_id=client_id, keys=keys, algorithms=algorithms, now=now, leeway=leeway
        )
    except SsoTokenInvalidError as exc:
        _log_rejection("logout_token", issuer, exc)
        raise


def _validate_logout_token(
    raw: str,
    *,
    issuer: str,
    client_id: str,
    keys: Iterable[Mapping[str, Any]],
    algorithms: Iterable[str],
    now: float | None,
    leeway: float | None,
) -> LogoutTokenClaims:
    current, skew = _resolve_time(now, leeway)
    payload = _decode(
        raw,
        issuer=issuer,
        client_id=client_id,
        keys=keys,
        algorithms=algorithms,
        required=_LOGOUT_TOKEN_REQUIRED,
        check_typ=_check_logout_typ,
    )
    exp, iat = _check_times(payload, current, skew)

    events = payload.get("events")
    if not isinstance(events, dict) or not isinstance(events.get(BACKCHANNEL_LOGOUT_EVENT), dict):
        raise _reject(TokenRejection.MISSING_EVENTS)
    if "nonce" in payload:
        raise _reject(TokenRejection.NONCE_PRESENT)

    jti = payload["jti"]  # PyJWT has checked it is a string
    if not jti:
        raise _reject(TokenRejection.MALFORMED, "jti")
    subject = _optional_identifier(payload, "sub")
    sid = _optional_identifier(payload, "sid")
    if subject is None and sid is None:
        raise _reject(TokenRejection.MISSING_SID_AND_SUB)

    return LogoutTokenClaims(
        issuer=payload["iss"],
        audience=_audiences(payload),
        issued_at=iat,
        expires_at=exp,
        jti=jti,
        subject=subject,
        sid=sid,
        claims=payload,
    )


# ---------------------------------------------------------------------------
# With JWKS lookup and key rotation
# ---------------------------------------------------------------------------


def _precheck_unverified(raw: str, *, issuer: str, client_id: str, now: float | None, leeway: float) -> None:
    """Cheap plausibility checks on the *unverified* claims of a token with an unknown ``kid``.

    Run before the rate-limited JWKS refetch is spent, so an unauthenticated
    token that could never validate (another issuer, another audience, out of
    its time window) cannot use up the refetch window a genuine token signed
    with a freshly rotated key needs. Raises the rejection the full
    validation would have raised.
    """
    try:
        payload = jwt.decode(raw, options={"verify_signature": False})
    except (PyJWTError, ValueError, TypeError):
        raise _reject(TokenRejection.MALFORMED, "payload") from None
    if not isinstance(payload, dict):  # pragma: no cover - PyJWT returns a dict or raises
        raise _reject(TokenRejection.MALFORMED, "payload")
    if payload.get("iss") != issuer:
        raise _reject(TokenRejection.WRONG_ISSUER)
    aud = payload.get("aud")
    audiences = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
    if client_id not in audiences:
        raise _reject(TokenRejection.WRONG_AUDIENCE)
    for name in ("exp", "iat"):
        if name not in payload:
            raise _reject(TokenRejection.MISSING_CLAIM, name)
    current, skew = _resolve_time(now, leeway)
    _check_times(payload, current, skew)


async def _with_jwks[T](
    validate: Callable[[list[dict[str, Any]], frozenset[str]], T],
    *,
    kind: str,
    raw: str,
    provider: SsoProvider,
    metadata: ProviderMetadata,
    redis: Redis | None,
    settings: Settings,
    now: float | None,
) -> T:
    """Validate with the cached JWKS; on an unknown ``kid``, refetch it once (key rotation).

    The refetch is rate limited per provider. Before spending it, the token's
    unverified ``iss``/``aud``/``iat``/``exp`` must be plausible. When the
    window is already spent, :class:`SsoSigningKeyRefreshLimitedError` says
    "try again" rather than "invalid": the kid may be a key the IdP has only
    just published, and a back-channel IdP retries a 503 but not a 400.
    """
    algorithms = signing_algorithms(metadata)
    keys = await get_jwks(provider.id, metadata.jwks_uri, redis=redis, settings=settings)
    try:
        return validate(keys, algorithms)
    except SsoUnknownSigningKeyError:
        try:
            _precheck_unverified(
                raw,
                issuer=provider.issuer,
                client_id=provider.client_id,
                now=now,
                leeway=settings.sso_clock_skew_seconds,
            )
        except SsoTokenInvalidError as exc:
            _log_rejection(kind, provider.issuer, exc)
            raise
        fresh = await refetch_jwks_for_unknown_kid(provider.id, metadata.jwks_uri, redis=redis, settings=settings)
        if fresh is None:
            raise SsoSigningKeyRefreshLimitedError("jwks_refetch_rate_limited") from None
        return validate(fresh, algorithms)


async def verify_id_token(
    raw: str,
    *,
    provider: SsoProvider,
    metadata: ProviderMetadata,
    nonce: str,
    access_token: str | None = None,
    redis: Redis | None = None,
    settings: Settings | None = None,
    now: float | None = None,
    max_age: int | None = None,
) -> IdTokenClaims:
    """:func:`validate_id_token` with the provider's JWKS (refetched once on an unknown kid)."""
    settings = settings or get_settings()
    return await _with_jwks(
        lambda keys, algorithms: validate_id_token(
            raw,
            issuer=provider.issuer,
            client_id=provider.client_id,
            keys=keys,
            algorithms=algorithms,
            nonce=nonce,
            access_token=access_token,
            now=now,
            leeway=settings.sso_clock_skew_seconds,
            max_age=max_age,
        ),
        kind="id_token",
        raw=raw,
        provider=provider,
        metadata=metadata,
        redis=redis,
        settings=settings,
        now=now,
    )


async def verify_logout_token(
    raw: str,
    *,
    provider: SsoProvider,
    metadata: ProviderMetadata,
    redis: Redis | None = None,
    settings: Settings | None = None,
    now: float | None = None,
) -> LogoutTokenClaims:
    """:func:`validate_logout_token` with the provider's JWKS (refetched once on an unknown kid)."""
    settings = settings or get_settings()
    return await _with_jwks(
        lambda keys, algorithms: validate_logout_token(
            raw,
            issuer=provider.issuer,
            client_id=provider.client_id,
            keys=keys,
            algorithms=algorithms,
            now=now,
            leeway=settings.sso_clock_skew_seconds,
        ),
        kind="logout_token",
        raw=raw,
        provider=provider,
        metadata=metadata,
        redis=redis,
        settings=settings,
        now=now,
    )
