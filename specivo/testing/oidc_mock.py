"""A strict mock OpenID Connect provider for tests, served through ``httpx.MockTransport``.

There is no network and no port: :class:`MockIdP` answers discovery, JWKS,
authorize, token, userinfo and end-session requests in-process, and the
``mock_idp`` fixture installs it behind the SSO HTTP client factory
(:func:`specivo.services.sso.http.override_transport`). It works from service
tests (no app) and from integration tests (ASGI client) alike.

The mock is deliberately strict, so the relying-party code is tested against
a counterpart that refuses what a real IdP refuses:

* the token endpoint checks ``grant_type``, single-use codes (a code is burnt
  on its first presentation, also when that attempt fails), code expiry,
  ``redirect_uri`` equality with the authorize request, PKCE S256 and client
  authentication (``client_secret_basic`` / ``client_secret_post`` /
  ``none``), and answers ``invalid_grant`` / ``invalid_client`` like a real IdP;
* only S256 PKCE is accepted when a code is issued;
* the refresh grant: every code exchange returns a refresh token (unless
  ``issue_refresh_tokens`` is off); presenting it answers like a real IdP,
  ``invalid_grant`` once :meth:`MockIdP.revoke_refresh_tokens` revoked it (the
  user was disabled or signed out), when it is unknown, or when
  ``refresh_grant_allowed`` is off (an IdP that issues refresh tokens the
  client may not use). With ``rotate_refresh_tokens`` each refresh returns a
  new token and the presented one stops working. ``grants`` counts the token
  requests per ``grant_type``.

Tokens: :meth:`MockIdP.mint_id_token` and :meth:`MockIdP.mint_logout_token`
sign valid tokens (``**overrides`` replace claims, :data:`OMIT` removes one).
:meth:`MockIdP.id_token_variant` and :meth:`MockIdP.logout_token_variant`
build broken ones by name; the names are in :data:`ID_TOKEN_VARIANTS` and
:data:`LOGOUT_TOKEN_VARIANTS`, for parametrised negative tests.

Keys: an RSA-2048 key (``kid=rsa-1``, RS256) and a P-256 key (``kid=ec-1``,
ES256). Key generation is slow, so key material comes from a small pool
generated once per test session and shared by every instance;
:meth:`MockIdP.rotate` publishes a new kid (optionally dropping the old ones)
on that instance only.

Faults: :meth:`MockIdP.set_fault` makes a route return a status, time out,
send an oversized body or redirect elsewhere; ``discovery_overrides`` changes
the discovery document (for example an issuer mismatch).

Example::

    async def test_login(mock_idp):
        verifier, challenge = pkce_pair()
        code = mock_idp.issue_code(nonce="n", code_challenge=challenge, redirect_uri=REDIRECT_URI)
        ...
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from specivo.services.sso.http import override_transport

DEFAULT_ISSUER = "https://idp.example.com/realms/test"
DEFAULT_CLIENT_ID = "specivo"
DEFAULT_CLIENT_SECRET = "test-client-secret-0123456789"

BACKCHANNEL_LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"

#: Route names accepted by :meth:`MockIdP.set_fault` and counted in ``MockIdP.calls``.
ROUTES = ("discovery", "jwks", "authorize", "token", "userinfo", "end_session")

_COMMON_VARIANTS = (
    "alg_none",
    "hs256_with_public_key",
    "unknown_kid",
    "rotated_key",
    "wrong_signing_key",
    "tampered",
    "expired",
    "future_iat",
    "wrong_iss",
    "wrong_aud",
)

#: Negative ID token variants. All are broken except ``groups_overage``, which
#: is validly signed and carries an Entra-style ``_claim_names`` overage marker.
ID_TOKEN_VARIANTS: tuple[str, ...] = _COMMON_VARIANTS + (
    "wrong_azp",
    "multi_aud_without_azp",
    "missing_sub",
    "missing_nonce",
    "wrong_nonce",
    "groups_overage",
)

#: Negative logout token variants (OIDC Back-Channel Logout 1.0, section 2.6).
LOGOUT_TOKEN_VARIANTS: tuple[str, ...] = _COMMON_VARIANTS + (
    "missing_jti",
    "missing_events",
    "missing_sid_and_sub",
    "with_nonce",
    "wrong_typ",
)

_TOKEN_ONLY_CLAIMS = frozenset({"iss", "aud", "exp", "iat", "nbf", "nonce", "azp", "auth_time", "sid", "at_hash"})
_PKCE_VERIFIER_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_KEY_POOL_SIZE = 3


class _Omit:
    def __repr__(self) -> str:
        return "OMIT"


#: Pass as a claim override to remove the claim from a minted token.
OMIT: Any = _Omit()


# ---------------------------------------------------------------------------
# Keys (generated once per test session)
# ---------------------------------------------------------------------------


@functools.cache
def _rsa_key(slot: int) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@functools.cache
def _ec_key(slot: int) -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


# Slot reserved for keys that are never published (wrong / retired key variants).
_SPARE_SLOT = -1


@dataclass(frozen=True)
class _SigningKey:
    kid: str
    family: str  # "RSA" or "EC"
    slot: int

    @property
    def alg(self) -> str:
        return "RS256" if self.family == "RSA" else "ES256"

    @property
    def private_key(self) -> rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey:
        return _rsa_key(self.slot) if self.family == "RSA" else _ec_key(self.slot)

    def public_jwk(self) -> dict[str, Any]:
        public = self.private_key.public_key()
        jwk: dict[str, Any]
        if isinstance(public, rsa.RSAPublicKey):
            jwk = RSAAlgorithm.to_jwk(public, as_dict=True)
        else:
            jwk = ECAlgorithm.to_jwk(public, as_dict=True)
        jwk.update(kid=self.kid, alg=self.alg, use="sig")
        return jwk

    def public_pem(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _json_segment(value: dict[str, Any]) -> str:
    return _b64url(json.dumps(value, separators=(",", ":")).encode())


def pkce_pair() -> tuple[str, str]:
    """A fresh ``(code_verifier, code_challenge)`` pair for PKCE S256."""
    verifier = secrets.token_urlsafe(48)
    return verifier, s256(verifier)


def s256(verifier: str) -> str:
    """The PKCE S256 challenge for ``verifier``."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def at_hash(access_token: str) -> str:
    """The ``at_hash`` claim for an access token (SHA-256 based algorithms)."""
    return _b64url(hashlib.sha256(access_token.encode("ascii")).digest()[:16])


def _apply(claims: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = {**claims, **overrides}
    return {k: v for k, v in merged.items() if v is not OMIT}


@dataclass
class _CodeRecord:
    client_id: str
    redirect_uri: str
    code_challenge: str
    nonce: str | None
    claims: dict[str, Any]
    id_token: str | None
    issued_at: float


@dataclass
class _Fault:
    status: int | None = None
    timeout: bool = False
    body_size: int | None = None
    redirect_to: str | None = None


@dataclass
class _OAuthError(Exception):
    error: str
    status: int = 400
    description: str = ""
    headers: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The mock provider
# ---------------------------------------------------------------------------


class MockIdP:
    """An in-process OpenID provider. See the module docstring."""

    def __init__(
        self,
        issuer: str = DEFAULT_ISSUER,
        client_id: str = DEFAULT_CLIENT_ID,
        client_secret: str | None = DEFAULT_CLIENT_SECRET,
        *,
        token_endpoint_auth_method: str = "client_secret_basic",
        clock: Callable[[], float] = time.time,
        code_ttl_seconds: int = 60,
        token_ttl_seconds: int = 300,
    ) -> None:
        if token_endpoint_auth_method not in ("client_secret_basic", "client_secret_post", "none"):
            raise ValueError(f"unsupported token_endpoint_auth_method {token_endpoint_auth_method!r}")
        if token_endpoint_auth_method != "none" and not client_secret:
            raise ValueError("a confidential client needs a client_secret")
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_endpoint_auth_method = token_endpoint_auth_method
        self.clock = clock
        self.code_ttl_seconds = code_ttl_seconds
        self.token_ttl_seconds = token_ttl_seconds

        # Neutral claim defaults; ``sub`` and ``sid`` are random per instance.
        self.default_sub = str(uuid.uuid4())
        self.default_sid = str(uuid.uuid4())
        self.default_nonce = secrets.token_urlsafe(16)
        self.default_claims: dict[str, Any] = {
            "email": "member@example.com",
            "email_verified": True,
            "name": "Example Member",
            "preferred_username": "member",
        }

        self.discovery_url = f"{self.issuer}/.well-known/openid-configuration"
        self.jwks_uri = f"{self.issuer}/protocol/openid-connect/certs"
        self.authorization_endpoint = f"{self.issuer}/protocol/openid-connect/auth"
        self.token_endpoint = f"{self.issuer}/protocol/openid-connect/token"
        self.userinfo_endpoint = f"{self.issuer}/protocol/openid-connect/userinfo"
        self.end_session_endpoint = f"{self.issuer}/protocol/openid-connect/logout"

        #: Merged into the discovery document; :data:`OMIT` removes a key.
        self.discovery_overrides: dict[str, Any] = {}
        #: Merged into every userinfo response (e.g. a different ``sub``).
        self.userinfo_overrides: dict[str, Any] = {}
        #: Claims used when the authorize route is hit over HTTP.
        self.authorize_claims: dict[str, Any] = {}
        #: ``Cache-Control`` header on the JWKS response.
        self.jwks_cache_control = "max-age=300"

        #: Refresh grant behaviour (see the module docstring).
        self.issue_refresh_tokens = True
        self.rotate_refresh_tokens = False
        self.refresh_grant_allowed = True

        #: Requests per route name (see :data:`ROUTES`), faulted ones included.
        self.calls: Counter[str] = Counter()
        #: Token requests per ``grant_type`` that got past client authentication.
        self.grants: Counter[str] = Counter()
        #: Query parameters of every end-session request, in order.
        self.end_session_requests: list[dict[str, str]] = []

        self._keys: list[_SigningKey] = [_SigningKey("rsa-1", "RSA", 0), _SigningKey("ec-1", "EC", 0)]
        self._active = {"RSA": "rsa-1", "EC": "ec-1"}
        self._generation = {"RSA": 1, "EC": 1}
        self._codes: dict[str, _CodeRecord] = {}
        self._access_tokens: dict[str, dict[str, Any]] = {}
        # Live refresh token -> the subject it was issued for.
        self._refresh_tokens: dict[str, str] = {}
        self._faults: dict[str, _Fault] = {}

        split = urlsplit(self.issuer)
        self._routes = {
            urlsplit(url).path: name
            for name, url in (
                ("discovery", self.discovery_url),
                ("jwks", self.jwks_uri),
                ("authorize", self.authorization_endpoint),
                ("token", self.token_endpoint),
                ("userinfo", self.userinfo_endpoint),
                ("end_session", self.end_session_endpoint),
            )
        }
        self._host = split.hostname
        self.transport = httpx.MockTransport(self._handle)

    # ------------------------------------------------------------------
    # Keys
    # ------------------------------------------------------------------

    def _key(self, kid: str) -> _SigningKey:
        for key in self._keys:
            if key.kid == kid:
                return key
        raise KeyError(kid)

    def _active_key(self, alg: str) -> _SigningKey:
        family = {"RS256": "RSA", "ES256": "EC"}.get(alg)
        if family is None:
            raise ValueError(f"MockIdP signs with RS256 or ES256, not {alg!r}")
        return self._key(self._active[family])

    def jwks(self) -> dict[str, Any]:
        """The published JWKS document (public keys only)."""
        return {"keys": [key.public_jwk() for key in self._keys]}

    def public_key_pem(self, kid: str) -> bytes:
        """PEM (SubjectPublicKeyInfo) of a published key."""
        return self._key(kid).public_pem()

    def rotate(self, *, drop_old: bool = False, family: str = "RSA") -> str:
        """Publish a new signing key for ``family`` ("RSA" or "EC") and make it active.

        With ``drop_old`` every older key of that family leaves the JWKS, so
        tokens signed with them no longer verify. Returns the new kid.
        """
        if family not in ("RSA", "EC"):
            raise ValueError("family must be 'RSA' or 'EC'")
        self._generation[family] += 1
        generation = self._generation[family]
        prefix = "rsa" if family == "RSA" else "ec"
        new = _SigningKey(f"{prefix}-{generation}", family, (generation - 1) % _KEY_POOL_SIZE)
        if drop_old:
            self._keys = [k for k in self._keys if k.family != family]
        self._keys.append(new)
        self._active[family] = new.kid
        return new.kid

    # ------------------------------------------------------------------
    # Minting
    # ------------------------------------------------------------------

    def _now(self) -> int:
        return int(self.clock())

    def id_token_claims(self, **overrides: Any) -> dict[str, Any]:
        """The claims :meth:`mint_id_token` would sign."""
        now = self._now()
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "sub": self.default_sub,
            "aud": self.client_id,
            "exp": now + self.token_ttl_seconds,
            "iat": now,
            "auth_time": now,
            "nonce": self.default_nonce,
            "sid": self.default_sid,
            **self.default_claims,
        }
        return _apply(claims, overrides)

    def logout_token_claims(self, **overrides: Any) -> dict[str, Any]:
        """The claims :meth:`mint_logout_token` would sign."""
        now = self._now()
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": self.client_id,
            "iat": now,
            "exp": now + 120,
            "jti": str(uuid.uuid4()),
            "events": {BACKCHANNEL_LOGOUT_EVENT: {}},
            "sid": self.default_sid,
            "sub": self.default_sub,
        }
        return _apply(claims, overrides)

    def _sign(self, claims: dict[str, Any], key: _SigningKey, *, typ: str = "JWT", kid: str | None = None) -> str:
        return jwt.encode(claims, key.private_key, algorithm=key.alg, headers={"kid": kid or key.kid, "typ": typ})

    def mint_id_token(self, *, alg: str = "RS256", kid: str | None = None, **overrides: Any) -> str:
        """A validly signed ID token. ``overrides`` replace claims; :data:`OMIT` removes one."""
        key = self._key(kid) if kid else self._active_key(alg)
        return self._sign(self.id_token_claims(**overrides), key)

    def mint_logout_token(self, *, alg: str = "RS256", kid: str | None = None, **overrides: Any) -> str:
        """A validly signed back-channel logout token (``typ=logout+jwt``)."""
        key = self._key(kid) if kid else self._active_key(alg)
        return self._sign(self.logout_token_claims(**overrides), key, typ="logout+jwt")

    def id_token_variant(self, name: str, **overrides: Any) -> str:
        """A broken ID token; ``name`` is one of :data:`ID_TOKEN_VARIANTS`."""
        if name not in ID_TOKEN_VARIANTS:
            raise KeyError(name)
        return self._variant(name, overrides, logout=False)

    def logout_token_variant(self, name: str, **overrides: Any) -> str:
        """A broken logout token; ``name`` is one of :data:`LOGOUT_TOKEN_VARIANTS`."""
        if name not in LOGOUT_TOKEN_VARIANTS:
            raise KeyError(name)
        return self._variant(name, overrides, logout=True)

    def _variant(self, name: str, overrides: dict[str, Any], *, logout: bool) -> str:
        build = self.logout_token_claims if logout else self.id_token_claims
        typ = "logout+jwt" if logout else "JWT"
        active = self._active_key("RS256")
        now = self._now()

        claim_changes: dict[str, dict[str, Any]] = {
            "expired": {"iat": now - 900, "exp": now - 600},
            "future_iat": {"iat": now + 3600, "exp": now + 3900},
            "wrong_iss": {"iss": "https://attacker.example.net/realms/test"},
            "wrong_aud": {"aud": "another-client"},
            "wrong_azp": {"aud": [self.client_id, "another-client"], "azp": "another-client"},
            "multi_aud_without_azp": {"aud": [self.client_id, "another-client"], "azp": OMIT},
            "missing_sub": {"sub": OMIT},
            "missing_nonce": {"nonce": OMIT},
            "wrong_nonce": {"nonce": "not-the-nonce-that-was-sent"},
            "groups_overage": {
                "groups": OMIT,
                "_claim_names": {"groups": "src1"},
                "_claim_sources": {
                    "src1": {"endpoint": "https://graph.example.com/v1.0/users/member/getMemberObjects"}
                },
            },
            "missing_jti": {"jti": OMIT},
            "missing_events": {"events": OMIT},
            "missing_sid_and_sub": {"sid": OMIT, "sub": OMIT},
            "with_nonce": {"nonce": "a-nonce"},
        }
        if name in claim_changes:
            return self._sign(build(**{**overrides, **claim_changes[name]}), active, typ=typ)

        claims = build(**overrides)
        if name == "wrong_typ":
            return self._sign(claims, active, typ="JWT")
        if name == "unknown_kid":
            return self._sign(claims, active, typ=typ, kid="unknown-kid")
        if name == "rotated_key":
            # Signed by a retired key whose kid is no longer published.
            return self._sign(claims, _SigningKey("rsa-0", "RSA", _SPARE_SLOT), typ=typ)
        if name == "wrong_signing_key":
            # A published kid, but signed by a different private key.
            return self._sign(claims, _SigningKey(active.kid, "RSA", _SPARE_SLOT), typ=typ)
        if name == "tampered":
            header, _, signature = self._sign(claims, active, typ=typ).split(".")
            changed = {**claims, "sub": f"{claims.get('sub', '')}-tampered"}
            return f"{header}.{_json_segment(changed)}.{signature}"
        if name == "alg_none":
            header = _json_segment({"alg": "none", "kid": active.kid, "typ": typ})
            return f"{header}.{_json_segment(claims)}."
        if name == "hs256_with_public_key":
            # Key confusion: HMAC keyed with the RSA public key PEM, which a
            # verifier that trusts the header ``alg`` would accept.
            signing_input = f"{_json_segment({'alg': 'HS256', 'kid': active.kid, 'typ': typ})}.{_json_segment(claims)}"
            mac = hmac.new(active.public_pem(), signing_input.encode(), hashlib.sha256).digest()
            return f"{signing_input}.{_b64url(mac)}"
        raise KeyError(name)  # pragma: no cover - names are validated by the callers

    # ------------------------------------------------------------------
    # Authorization codes
    # ------------------------------------------------------------------

    def issue_code(
        self,
        *,
        nonce: str | None,
        code_challenge: str,
        redirect_uri: str,
        claims: dict[str, Any] | None = None,
        id_token: str | None = None,
        code_challenge_method: str = "S256",
        client_id: str | None = None,
    ) -> str:
        """Issue an authorization code as if the user had signed in.

        ``claims`` go into the ID token minted at exchange time; ``id_token``
        replaces that token verbatim (for negative tests). ``nonce=None``
        leaves the nonce claim out.
        """
        if code_challenge_method != "S256":
            raise ValueError("MockIdP only accepts PKCE S256")
        if not code_challenge:
            raise ValueError("code_challenge is required")
        code = secrets.token_urlsafe(32)
        self._codes[code] = _CodeRecord(
            client_id=client_id or self.client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            nonce=nonce,
            claims=dict(claims or {}),
            id_token=id_token,
            issued_at=self.clock(),
        )
        return code

    def authorize(
        self,
        authorize_url: str,
        *,
        claims: dict[str, Any] | None = None,
        id_token_variant: str | None = None,
        error: str | None = None,
    ) -> str:
        """Play the IdP side of an authorize redirect and return the callback URL.

        Validates the request like a strict IdP (``ValueError`` otherwise),
        issues a code bound to its ``redirect_uri``, ``nonce`` and PKCE
        challenge, and returns ``redirect_uri?code=..&state=..&iss=..``. With
        ``error`` the callback carries ``error=`` instead of a code; with
        ``id_token_variant`` the code exchanges for that broken ID token.
        """
        params = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query, keep_blank_values=True).items()}
        if params.get("response_type") != "code":
            raise ValueError("response_type must be 'code'")
        if params.get("client_id") != self.client_id:
            raise ValueError("unknown client_id")
        redirect_uri = params.get("redirect_uri")
        if not redirect_uri:
            raise ValueError("redirect_uri is required")
        if "openid" not in params.get("scope", "").split():
            raise ValueError("scope must include openid")
        if not params.get("code_challenge") or params.get("code_challenge_method") != "S256":
            raise ValueError("PKCE S256 is required")

        result: dict[str, str] = {}
        if error:
            result["error"] = error
        else:
            nonce = params.get("nonce")
            id_token = None
            if id_token_variant:
                variant_overrides: dict[str, Any] = {"nonce": nonce if nonce is not None else OMIT}
                variant_overrides.update(claims or {})
                id_token = self.id_token_variant(id_token_variant, **variant_overrides)
            result["code"] = self.issue_code(
                nonce=nonce,
                code_challenge=params["code_challenge"],
                redirect_uri=redirect_uri,
                claims=claims,
                id_token=id_token,
            )
        if "state" in params:
            result["state"] = params["state"]
        result["iss"] = self.issuer
        separator = "&" if "?" in redirect_uri else "?"
        return f"{redirect_uri}{separator}{urlencode(result)}"

    # ------------------------------------------------------------------
    # Refresh tokens
    # ------------------------------------------------------------------

    def issue_refresh_token(self, sub: str | None = None) -> str:
        """A live refresh token for ``sub`` (default: :attr:`default_sub`), as if a code exchange issued it."""
        token = secrets.token_urlsafe(32)
        self._refresh_tokens[token] = sub or self.default_sub
        return token

    def revoke_refresh_tokens(self, sub: str | None = None) -> int:
        """Refuse every refresh token (of ``sub`` only, if given) from now on, as after a disable or sign-out."""
        dead = [t for t, owner in self._refresh_tokens.items() if sub is None or owner == sub]
        for token in dead:
            del self._refresh_tokens[token]
        return len(dead)

    @property
    def live_refresh_tokens(self) -> int:
        return len(self._refresh_tokens)

    # ------------------------------------------------------------------
    # Faults
    # ------------------------------------------------------------------

    def set_fault(
        self,
        route: str,
        *,
        status: int | None = None,
        timeout: bool = False,
        body_size: int | None = None,
        redirect_to: str | None = None,
    ) -> None:
        """Make ``route`` (one of :data:`ROUTES`) misbehave until :meth:`clear_faults`.

        ``timeout`` raises ``httpx.ReadTimeout``; ``redirect_to`` answers 302;
        ``body_size`` sends that many bytes; ``status`` sets the status code.
        """
        if route not in ROUTES:
            raise KeyError(route)
        self._faults[route] = _Fault(status=status, timeout=timeout, body_size=body_size, redirect_to=redirect_to)

    def clear_faults(self) -> None:
        self._faults.clear()

    # ------------------------------------------------------------------
    # HTTP handling
    # ------------------------------------------------------------------

    def discovery_document(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "issuer": self.issuer,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "userinfo_endpoint": self.userinfo_endpoint,
            "jwks_uri": self.jwks_uri,
            "end_session_endpoint": self.end_session_endpoint,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256", "ES256"],
            "scopes_supported": ["openid", "profile", "email"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
            "code_challenge_methods_supported": ["S256"],
            "claims_supported": ["sub", "iss", "aud", "email", "email_verified", "name", "preferred_username"],
            "backchannel_logout_supported": True,
            "backchannel_logout_session_supported": True,
            "authorization_response_iss_parameter_supported": True,
        }
        return _apply(doc, self.discovery_overrides)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        route = self._routes.get(request.url.path) if request.url.host == self._host else None
        if route is None:
            return httpx.Response(404, json={"error": "not_found"})
        self.calls[route] += 1

        fault = self._faults.get(route)
        if fault is not None:
            if fault.timeout:
                raise httpx.ReadTimeout("mock IdP timeout", request=request)
            if fault.redirect_to:
                return httpx.Response(302, headers={"Location": fault.redirect_to})
            if fault.body_size is not None:
                return httpx.Response(fault.status or 200, content=b"x" * fault.body_size)
            if fault.status is not None:
                return httpx.Response(fault.status, json={"error": "server_error"})

        try:
            if route == "discovery":
                return httpx.Response(200, json=self.discovery_document())
            if route == "jwks":
                return httpx.Response(200, json=self.jwks(), headers={"Cache-Control": self.jwks_cache_control})
            if route == "authorize":
                return self._handle_authorize(request)
            if route == "token":
                return self._handle_token(request)
            if route == "userinfo":
                return self._handle_userinfo(request)
            return self._handle_end_session(request)
        except _OAuthError as exc:
            body = {"error": exc.error}
            if exc.description:
                body["error_description"] = exc.description
            return httpx.Response(exc.status, json=body, headers={"Cache-Control": "no-store", **exc.headers})

    def _handle_authorize(self, request: httpx.Request) -> httpx.Response:
        try:
            location = self.authorize(str(request.url), claims=self.authorize_claims or None)
        except ValueError as exc:
            raise _OAuthError("invalid_request", description=str(exc)) from exc
        return httpx.Response(302, headers={"Location": location})

    def _form(self, request: httpx.Request) -> dict[str, str]:
        if request.method != "POST":
            raise _OAuthError("invalid_request", status=405, description="POST required")
        parsed = parse_qs(request.content.decode(), keep_blank_values=True)
        if any(len(values) > 1 for values in parsed.values()):
            raise _OAuthError("invalid_request", description="repeated parameter")
        return {k: v[0] for k, v in parsed.items()}

    def _authenticate_client(self, request: httpx.Request, form: dict[str, str]) -> None:
        invalid = _OAuthError("invalid_client", status=401, headers={"WWW-Authenticate": 'Basic realm="mock-idp"'})
        authorization = request.headers.get("Authorization", "")
        method = self.token_endpoint_auth_method

        if method == "client_secret_basic":
            if not authorization.lower().startswith("basic ") or "client_secret" in form:
                raise invalid
            try:
                decoded = base64.b64decode(authorization[6:].strip(), validate=True).decode()
            except (ValueError, UnicodeDecodeError) as exc:
                raise invalid from exc
            raw_id, sep, raw_secret = decoded.partition(":")
            client_id, secret = unquote_plus(raw_id), unquote_plus(raw_secret)
            if not sep or client_id != self.client_id or not self._secret_matches(secret):
                raise invalid
            if form.get("client_id", client_id) != client_id:
                raise invalid
            return

        if authorization:
            raise invalid
        if form.get("client_id") != self.client_id:
            raise invalid
        if method == "client_secret_post":
            if not self._secret_matches(form.get("client_secret", "")):
                raise invalid
        elif "client_secret" in form:
            raise invalid

    def _secret_matches(self, secret: str) -> bool:
        expected = self.client_secret or ""
        return bool(expected) and hmac.compare_digest(secret.encode(), expected.encode())

    def _handle_token(self, request: httpx.Request) -> httpx.Response:
        form = self._form(request)
        self._authenticate_client(request, form)
        grant_type = form.get("grant_type", "")
        self.grants[grant_type] += 1
        if grant_type == "refresh_token":
            return self._handle_refresh(form)
        if grant_type != "authorization_code":
            raise _OAuthError("unsupported_grant_type")

        # Single use: the code is burnt on its first presentation.
        record = self._codes.pop(form.get("code", ""), None)
        if record is None:
            raise _OAuthError("invalid_grant", description="unknown or used code")
        if self.clock() - record.issued_at > self.code_ttl_seconds:
            raise _OAuthError("invalid_grant", description="code expired")
        if record.client_id != self.client_id:
            raise _OAuthError("invalid_grant", description="code was issued to another client")
        if form.get("redirect_uri") != record.redirect_uri:
            raise _OAuthError("invalid_grant", description="redirect_uri mismatch")
        verifier = form.get("code_verifier", "")
        if not _PKCE_VERIFIER_RE.match(verifier) or not hmac.compare_digest(s256(verifier), record.code_challenge):
            raise _OAuthError("invalid_grant", description="PKCE verification failed")

        access_token = secrets.token_urlsafe(32)
        if record.id_token is not None:
            id_token = record.id_token
        else:
            nonce: Any = record.nonce if record.nonce is not None else OMIT
            id_token = self.mint_id_token(nonce=nonce, at_hash=at_hash(access_token), **record.claims)

        profile = {**self.default_claims, "sub": self.default_sub, **record.claims}
        self._access_tokens[access_token] = {
            k: v for k, v in profile.items() if k not in _TOKEN_ONLY_CLAIMS and v is not OMIT
        }
        body = {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self.token_ttl_seconds,
            "id_token": id_token,
            "scope": "openid profile email",
        }
        if self.issue_refresh_tokens:
            body["refresh_token"] = self.issue_refresh_token(str(profile.get("sub") or self.default_sub))
        return httpx.Response(200, json=body, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    def _handle_refresh(self, form: dict[str, str]) -> httpx.Response:
        token = form.get("refresh_token", "")
        sub = self._refresh_tokens.get(token)
        if sub is None or not self.refresh_grant_allowed:
            raise _OAuthError("invalid_grant", description="Token is not active")
        body: dict[str, Any] = {
            "access_token": secrets.token_urlsafe(32),
            "token_type": "Bearer",
            "expires_in": self.token_ttl_seconds,
            "id_token": self.mint_id_token(sub=sub, nonce=OMIT),
        }
        if self.rotate_refresh_tokens:
            del self._refresh_tokens[token]
            body["refresh_token"] = self.issue_refresh_token(sub)
        else:
            body["refresh_token"] = token
        return httpx.Response(200, json=body, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    def _handle_userinfo(self, request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("Authorization", "")
        token = authorization[7:] if authorization.lower().startswith("bearer ") else ""
        claims = self._access_tokens.get(token)
        if claims is None:
            raise _OAuthError("invalid_token", status=401, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
        return httpx.Response(200, json=_apply(claims, self.userinfo_overrides))

    def _handle_end_session(self, request: httpx.Request) -> httpx.Response:
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode(), keep_blank_values=True).items()}
        self.end_session_requests.append(params)
        target = params.get("post_logout_redirect_uri")
        if not target:
            return httpx.Response(200, text="Signed out")
        if "state" in params:
            target += ("&" if "?" in target else "?") + urlencode({"state": params["state"]})
        return httpx.Response(302, headers={"Location": target})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_idp() -> Iterator[MockIdP]:
    """A fresh :class:`MockIdP`, installed behind the SSO HTTP client factory."""
    idp = MockIdP()
    with override_transport(idp.transport):
        yield idp
