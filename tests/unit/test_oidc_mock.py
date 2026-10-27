"""Self-tests for the mock OpenID Connect provider in ``specivo.testing.oidc_mock``.

The mock is the permanent regression harness for the SSO code, so it has to be
a strict counterpart: these tests prove that its token endpoint refuses what a
real IdP refuses, that tokens it mints verify against its own JWKS, and that
every negative variant is really broken in the way its name says.

The reference verifiers below use PyJWT directly. They are not Specivo's
validator; they only show that the mock's output is what it claims to be.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest

from specivo.services.sso.http import (
    SSORedirectError,
    SSOResponseTooLargeError,
    create_sso_client,
    override_transport,
)
from specivo.testing.oidc_mock import (
    BACKCHANNEL_LOGOUT_EVENT,
    DEFAULT_CLIENT_ID,
    DEFAULT_CLIENT_SECRET,
    DEFAULT_ISSUER,
    ID_TOKEN_VARIANTS,
    LOGOUT_TOKEN_VARIANTS,
    OMIT,
    MockIdP,
    pkce_pair,
)

pytestmark = pytest.mark.unit

REDIRECT_URI = "https://specivo.example.com/auth/oidc/test/callback/"
ALLOWED_ALGS = ["RS256", "ES256"]


# ---------------------------------------------------------------------------
# Reference verifiers (PyJWT only)
# ---------------------------------------------------------------------------


class AzpError(Exception):
    pass


class NonceError(Exception):
    pass


class LogoutTokenError(Exception):
    pass


def _key_for(idp: MockIdP, token: str) -> jwt.PyJWK:
    jwks = jwt.PyJWKSet.from_dict(idp.jwks())
    kid = jwt.get_unverified_header(token).get("kid")
    try:
        return jwks[kid]
    except KeyError as exc:
        raise LookupError(f"no key with kid {kid!r}") from exc


def verify_id_token(idp: MockIdP, token: str, *, nonce: str | None = None) -> dict[str, Any]:
    claims = jwt.decode(
        token,
        key=_key_for(idp, token),
        algorithms=ALLOWED_ALGS,
        audience=idp.client_id,
        issuer=idp.issuer,
        options={"require": ["iss", "aud", "exp", "iat", "sub"]},
    )
    aud = claims["aud"]
    if ("azp" in claims or (isinstance(aud, list) and len(aud) > 1)) and claims.get("azp") != idp.client_id:
        raise AzpError("azp does not match client_id")
    expected_nonce = idp.default_nonce if nonce is None else nonce
    if claims.get("nonce") != expected_nonce:
        raise NonceError("nonce mismatch")
    return claims


def verify_logout_token(idp: MockIdP, token: str) -> dict[str, Any]:
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(
        token,
        key=_key_for(idp, token),
        algorithms=ALLOWED_ALGS,
        audience=idp.client_id,
        issuer=idp.issuer,
        options={"require": ["iss", "aud", "iat", "jti"]},
    )
    if "typ" in header and header["typ"] != "logout+jwt":
        raise LogoutTokenError("typ")
    if BACKCHANNEL_LOGOUT_EVENT not in (claims.get("events") or {}):
        raise LogoutTokenError("events")
    if "sid" not in claims and "sub" not in claims:
        raise LogoutTokenError("sid/sub")
    if "nonce" in claims:
        raise LogoutTokenError("nonce")
    return claims


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _segments(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    header, payload, signature = token.split(".")
    return json.loads(_b64url_decode(header)), json.loads(_b64url_decode(payload)), _b64url_decode(signature)


async def _exchange(idp: MockIdP, form: dict[str, str], auth: tuple[str, str] | None = None) -> httpx.Response:
    async with create_sso_client() as client:
        return await client.post(idp.token_endpoint, data=form, auth=auth)


def _code_form(code: str, verifier: str, redirect_uri: str = REDIRECT_URI) -> dict[str, str]:
    return {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }


def _issue(idp: MockIdP, **kwargs: Any) -> tuple[str, str]:
    verifier, challenge = pkce_pair()
    code = idp.issue_code(
        nonce=kwargs.pop("nonce", "n-123"),
        code_challenge=challenge,
        redirect_uri=kwargs.pop("redirect_uri", REDIRECT_URI),
        **kwargs,
    )
    return code, verifier


BASIC = (DEFAULT_CLIENT_ID, DEFAULT_CLIENT_SECRET)


# ---------------------------------------------------------------------------
# Defaults, discovery, JWKS
# ---------------------------------------------------------------------------


class TestDiscoveryAndJwks:
    async def test_defaults(self, mock_idp: MockIdP) -> None:
        assert mock_idp.issuer == DEFAULT_ISSUER == "https://idp.example.com/realms/test"
        assert mock_idp.client_id == "specivo"
        assert mock_idp.discovery_url == f"{DEFAULT_ISSUER}/.well-known/openid-configuration"

    async def test_discovery_document(self, mock_idp: MockIdP) -> None:
        async with create_sso_client() as client:
            doc = (await client.get(mock_idp.discovery_url)).json()
        assert doc["issuer"] == mock_idp.issuer
        assert doc["jwks_uri"] == mock_idp.jwks_uri
        assert doc["token_endpoint"] == mock_idp.token_endpoint
        assert doc["authorization_endpoint"] == mock_idp.authorization_endpoint
        assert doc["userinfo_endpoint"] == mock_idp.userinfo_endpoint
        assert doc["end_session_endpoint"] == mock_idp.end_session_endpoint
        assert doc["code_challenge_methods_supported"] == ["S256"]
        assert set(doc["id_token_signing_alg_values_supported"]) == {"RS256", "ES256"}
        assert doc["backchannel_logout_supported"] is True
        assert doc["authorization_response_iss_parameter_supported"] is True
        assert mock_idp.calls["discovery"] == 1

    async def test_discovery_issuer_mismatch_switch(self, mock_idp: MockIdP) -> None:
        mock_idp.discovery_overrides["issuer"] = "https://other.example.com/realms/test"
        async with create_sso_client() as client:
            doc = (await client.get(mock_idp.discovery_url)).json()
        assert doc["issuer"] == "https://other.example.com/realms/test"

    async def test_jwks_has_rsa_and_ec_public_keys_only(self, mock_idp: MockIdP) -> None:
        async with create_sso_client() as client:
            jwks = (await client.get(mock_idp.jwks_uri)).json()
        by_kid = {k["kid"]: k for k in jwks["keys"]}
        assert by_kid["rsa-1"]["kty"] == "RSA"
        assert by_kid["ec-1"]["kty"] == "EC"
        assert by_kid["ec-1"]["crv"] == "P-256"
        assert all(k["use"] == "sig" for k in jwks["keys"])
        assert all("d" not in k for k in jwks["keys"]), "private key material leaked"
        assert int.from_bytes(_b64url_decode(by_kid["rsa-1"]["n"])).bit_length() == 2048

    async def test_request_counters(self, mock_idp: MockIdP) -> None:
        async with create_sso_client() as client:
            await client.get(mock_idp.jwks_uri)
            await client.get(mock_idp.jwks_uri)
        assert mock_idp.calls["jwks"] == 2
        assert mock_idp.calls["discovery"] == 0

    async def test_keys_are_shared_between_instances(self) -> None:
        """Key generation is slow, so keys are generated once per test session."""
        assert MockIdP().jwks() == MockIdP().jwks()

    async def test_unknown_path_is_404(self, mock_idp: MockIdP) -> None:
        async with create_sso_client() as client:
            resp = await client.get(f"{mock_idp.issuer}/nope")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Minted tokens
# ---------------------------------------------------------------------------


class TestMintIdToken:
    def test_rs256_token_verifies_with_jwks(self) -> None:
        idp = MockIdP()
        claims = verify_id_token(idp, idp.mint_id_token())
        assert claims["iss"] == idp.issuer
        assert claims["aud"] == idp.client_id
        assert claims["email"] == "member@example.com"
        assert claims["name"] == "Example Member"
        assert claims["preferred_username"] == "member"
        assert claims["email_verified"] is True
        assert claims["sub"] == idp.default_sub
        assert jwt.get_unverified_header(idp.mint_id_token())["kid"] == "rsa-1"

    def test_es256_token_verifies_with_jwks(self) -> None:
        idp = MockIdP()
        token = idp.mint_id_token(alg="ES256")
        assert jwt.get_unverified_header(token) == {"alg": "ES256", "kid": "ec-1", "typ": "JWT"}
        verify_id_token(idp, token)

    def test_default_sub_is_a_random_uuid_per_instance(self) -> None:
        assert MockIdP().default_sub != MockIdP().default_sub

    def test_overrides_and_omit(self) -> None:
        idp = MockIdP()
        token = idp.mint_id_token(email="other@example.com", groups=["specivo-users"], name=OMIT)
        claims = verify_id_token(idp, token)
        assert claims["email"] == "other@example.com"
        assert claims["groups"] == ["specivo-users"]
        assert "name" not in claims

    def test_rotate_keeps_old_key_until_dropped(self) -> None:
        idp = MockIdP()
        old = idp.mint_id_token()
        new_kid = idp.rotate()
        new = idp.mint_id_token()
        assert jwt.get_unverified_header(new)["kid"] == new_kid != "rsa-1"
        verify_id_token(idp, old)
        verify_id_token(idp, new)

    def test_rotate_with_drop_old_breaks_old_tokens(self) -> None:
        idp = MockIdP()
        old = idp.mint_id_token()
        idp.rotate(drop_old=True)
        assert "rsa-1" not in {k["kid"] for k in idp.jwks()["keys"]}
        assert "ec-1" in {k["kid"] for k in idp.jwks()["keys"]}
        with pytest.raises(LookupError):
            verify_id_token(idp, old)
        verify_id_token(idp, idp.mint_id_token())

    def test_rotate_ec_family(self) -> None:
        idp = MockIdP()
        kid = idp.rotate(family="EC", drop_old=True)
        token = idp.mint_id_token(alg="ES256")
        assert jwt.get_unverified_header(token)["kid"] == kid
        verify_id_token(idp, token)

    def test_rotation_on_one_instance_does_not_leak_into_another(self) -> None:
        idp = MockIdP()
        idp.rotate(drop_old=True)
        assert "rsa-1" in {k["kid"] for k in MockIdP().jwks()["keys"]}


class TestIdTokenVariants:
    EXPECTED: dict[str, type[Exception]] = {
        "alg_none": jwt.InvalidAlgorithmError,
        "hs256_with_public_key": jwt.InvalidAlgorithmError,
        "unknown_kid": LookupError,
        "rotated_key": LookupError,
        "wrong_signing_key": jwt.InvalidSignatureError,
        "tampered": jwt.InvalidSignatureError,
        "expired": jwt.ExpiredSignatureError,
        "future_iat": jwt.ImmatureSignatureError,
        "wrong_iss": jwt.InvalidIssuerError,
        "wrong_aud": jwt.InvalidAudienceError,
        "wrong_azp": AzpError,
        "multi_aud_without_azp": AzpError,
        "missing_sub": jwt.MissingRequiredClaimError,
        "missing_nonce": NonceError,
        "wrong_nonce": NonceError,
    }

    def test_every_negative_variant_has_an_expectation(self) -> None:
        assert set(ID_TOKEN_VARIANTS) == set(self.EXPECTED) | {"groups_overage"}

    @pytest.mark.parametrize("variant", sorted(EXPECTED))
    def test_variant_is_rejected_for_the_expected_reason(self, variant: str) -> None:
        idp = MockIdP()
        token = idp.id_token_variant(variant)
        with pytest.raises(self.EXPECTED[variant]):
            verify_id_token(idp, token)

    def test_variant_rejects_unknown_name(self) -> None:
        with pytest.raises(KeyError):
            MockIdP().id_token_variant("no-such-variant")

    def test_variant_accepts_claim_overrides(self) -> None:
        idp = MockIdP()
        token = idp.id_token_variant("expired", nonce="n-1", email="x@example.com")
        _, payload, _ = _segments(token)
        assert payload["nonce"] == "n-1"
        assert payload["email"] == "x@example.com"

    def test_alg_none_has_an_empty_signature(self) -> None:
        header, _, signature = _segments(MockIdP().id_token_variant("alg_none"))
        assert header["alg"] == "none"
        assert signature == b""

    def test_hs256_variant_is_a_real_hmac_over_the_public_key(self) -> None:
        """A verifier that trusted the header alg would accept it: the attack is real."""
        idp = MockIdP()
        token = idp.id_token_variant("hs256_with_public_key")
        header, _, signature = _segments(token)
        assert header["alg"] == "HS256"
        assert header["kid"] == "rsa-1"
        signing_input = token.rsplit(".", 1)[0].encode()
        expected = hmac.new(idp.public_key_pem("rsa-1"), signing_input, hashlib.sha256).digest()
        assert hmac.compare_digest(signature, expected)

    def test_tampered_variant_carries_a_changed_payload(self) -> None:
        idp = MockIdP()
        _, payload, _ = _segments(idp.id_token_variant("tampered"))
        assert payload["sub"] != idp.default_sub

    def test_wrong_signing_key_uses_a_published_kid(self) -> None:
        idp = MockIdP()
        header, _, _ = _segments(idp.id_token_variant("wrong_signing_key"))
        assert header["kid"] in {k["kid"] for k in idp.jwks()["keys"]}

    def test_groups_overage_is_validly_signed_but_has_no_groups(self) -> None:
        idp = MockIdP()
        claims = verify_id_token(idp, idp.id_token_variant("groups_overage"))
        assert "groups" not in claims
        source = claims["_claim_names"]["groups"]
        assert "endpoint" in claims["_claim_sources"][source]


class TestLogoutTokens:
    def test_valid_logout_token(self) -> None:
        idp = MockIdP()
        token = idp.mint_logout_token()
        claims = verify_logout_token(idp, token)
        assert jwt.get_unverified_header(token)["typ"] == "logout+jwt"
        assert claims["sid"] == idp.default_sid
        assert claims["sub"] == idp.default_sub
        assert claims["events"] == {BACKCHANNEL_LOGOUT_EVENT: {}}
        assert "nonce" not in claims

    def test_jti_is_unique_per_token(self) -> None:
        idp = MockIdP()
        a = verify_logout_token(idp, idp.mint_logout_token())
        b = verify_logout_token(idp, idp.mint_logout_token())
        assert a["jti"] != b["jti"]

    def test_sid_only_and_sub_only(self) -> None:
        idp = MockIdP()
        assert "sub" not in verify_logout_token(idp, idp.mint_logout_token(sub=OMIT))
        assert "sid" not in verify_logout_token(idp, idp.mint_logout_token(sid=OMIT))

    EXPECTED: dict[str, type[Exception]] = {
        "alg_none": jwt.InvalidAlgorithmError,
        "hs256_with_public_key": jwt.InvalidAlgorithmError,
        "unknown_kid": LookupError,
        "rotated_key": LookupError,
        "wrong_signing_key": jwt.InvalidSignatureError,
        "tampered": jwt.InvalidSignatureError,
        "expired": jwt.ExpiredSignatureError,
        "future_iat": jwt.ImmatureSignatureError,
        "wrong_iss": jwt.InvalidIssuerError,
        "wrong_aud": jwt.InvalidAudienceError,
        "missing_jti": jwt.MissingRequiredClaimError,
        "missing_events": LogoutTokenError,
        "missing_sid_and_sub": LogoutTokenError,
        "with_nonce": LogoutTokenError,
        "wrong_typ": LogoutTokenError,
    }

    def test_every_logout_variant_has_an_expectation(self) -> None:
        assert set(LOGOUT_TOKEN_VARIANTS) == set(self.EXPECTED)

    @pytest.mark.parametrize("variant", sorted(EXPECTED))
    def test_logout_variant_is_rejected(self, variant: str) -> None:
        idp = MockIdP()
        with pytest.raises(self.EXPECTED[variant]):
            verify_logout_token(idp, idp.logout_token_variant(variant))


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------


class TestTokenEndpoint:
    async def test_successful_exchange(self, mock_idp: MockIdP) -> None:
        code, verifier = _issue(mock_idp, nonce="n-abc", claims={"groups": ["specivo-users"]})
        resp = await _exchange(mock_idp, _code_form(code, verifier), auth=BASIC)
        assert resp.status_code == 200, resp.text
        assert resp.headers["Cache-Control"] == "no-store"
        body = resp.json()
        assert body["token_type"] == "Bearer"
        claims = verify_id_token(mock_idp, body["id_token"], nonce="n-abc")
        assert claims["groups"] == ["specivo-users"]
        digest = hashlib.sha256(body["access_token"].encode()).digest()
        assert claims["at_hash"] == base64.urlsafe_b64encode(digest[:16]).rstrip(b"=").decode()
        assert mock_idp.calls["token"] == 1

    async def test_reused_code_is_invalid_grant(self, mock_idp: MockIdP) -> None:
        code, verifier = _issue(mock_idp)
        assert (await _exchange(mock_idp, _code_form(code, verifier), auth=BASIC)).status_code == 200
        resp = await _exchange(mock_idp, _code_form(code, verifier), auth=BASIC)
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_grant"

    async def test_wrong_verifier_is_invalid_grant(self, mock_idp: MockIdP) -> None:
        code, _ = _issue(mock_idp)
        other_verifier, _ = pkce_pair()
        resp = await _exchange(mock_idp, _code_form(code, other_verifier), auth=BASIC)
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_grant"

    async def test_missing_verifier_is_invalid_grant(self, mock_idp: MockIdP) -> None:
        code, _ = _issue(mock_idp)
        form = _code_form(code, "")
        del form["code_verifier"]
        resp = await _exchange(mock_idp, form, auth=BASIC)
        assert resp.json()["error"] == "invalid_grant"

    async def test_failed_attempt_burns_the_code(self, mock_idp: MockIdP) -> None:
        code, verifier = _issue(mock_idp)
        await _exchange(mock_idp, _code_form(code, "x" * 43), auth=BASIC)
        resp = await _exchange(mock_idp, _code_form(code, verifier), auth=BASIC)
        assert resp.json()["error"] == "invalid_grant"

    async def test_wrong_redirect_uri_is_invalid_grant(self, mock_idp: MockIdP) -> None:
        code, verifier = _issue(mock_idp)
        resp = await _exchange(mock_idp, _code_form(code, verifier, REDIRECT_URI + "x"), auth=BASIC)
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_grant"

    async def test_unknown_code_is_invalid_grant(self, mock_idp: MockIdP) -> None:
        verifier, _ = pkce_pair()
        resp = await _exchange(mock_idp, _code_form("nope", verifier), auth=BASIC)
        assert resp.json()["error"] == "invalid_grant"

    async def test_expired_code_is_invalid_grant(self) -> None:
        now = [1_000_000.0]
        idp = MockIdP(clock=lambda: now[0])
        code, verifier = _issue(idp)
        now[0] += idp.code_ttl_seconds + 1
        with override_transport(idp.transport):
            resp = await _exchange(idp, _code_form(code, verifier), auth=BASIC)
        assert resp.json()["error"] == "invalid_grant"

    async def test_wrong_grant_type(self, mock_idp: MockIdP) -> None:
        code, verifier = _issue(mock_idp)
        form = _code_form(code, verifier) | {"grant_type": "password"}
        resp = await _exchange(mock_idp, form, auth=BASIC)
        assert resp.status_code == 400
        assert resp.json()["error"] == "unsupported_grant_type"

    async def test_get_is_not_allowed(self, mock_idp: MockIdP) -> None:
        async with create_sso_client() as client:
            resp = await client.get(mock_idp.token_endpoint)
        assert resp.status_code == 405

    async def test_wrong_client_secret_is_invalid_client(self, mock_idp: MockIdP) -> None:
        code, verifier = _issue(mock_idp)
        resp = await _exchange(mock_idp, _code_form(code, verifier), auth=(DEFAULT_CLIENT_ID, "wrong"))
        assert resp.status_code == 401
        assert resp.json()["error"] == "invalid_client"
        assert "WWW-Authenticate" in resp.headers

    async def test_missing_client_auth_is_invalid_client(self, mock_idp: MockIdP) -> None:
        code, verifier = _issue(mock_idp)
        resp = await _exchange(mock_idp, _code_form(code, verifier))
        assert resp.status_code == 401
        assert resp.json()["error"] == "invalid_client"

    async def test_basic_credentials_are_form_urlencoded(self) -> None:
        """RFC 6749 2.3.1: id and secret are form-urlencoded before base64."""
        idp = MockIdP(client_secret="s3cret:with/odd+chars")
        code, verifier = _issue(idp)
        raw = "specivo:" + "s3cret%3Awith%2Fodd%2Bchars"
        headers = {"Authorization": "Basic " + base64.b64encode(raw.encode()).decode()}
        with override_transport(idp.transport):
            async with create_sso_client() as client:
                resp = await client.post(idp.token_endpoint, data=_code_form(code, verifier), headers=headers)
        assert resp.status_code == 200, resp.text

    async def test_client_secret_post(self) -> None:
        idp = MockIdP(token_endpoint_auth_method="client_secret_post")
        code, verifier = _issue(idp)
        form = _code_form(code, verifier) | {"client_id": idp.client_id, "client_secret": idp.client_secret}
        with override_transport(idp.transport):
            assert (await _exchange(idp, form)).status_code == 200

    async def test_client_secret_post_refuses_basic(self) -> None:
        idp = MockIdP(token_endpoint_auth_method="client_secret_post")
        code, verifier = _issue(idp)
        with override_transport(idp.transport):
            resp = await _exchange(idp, _code_form(code, verifier), auth=BASIC)
        assert resp.json()["error"] == "invalid_client"

    async def test_public_client_none(self) -> None:
        idp = MockIdP(token_endpoint_auth_method="none", client_secret=None)
        code, verifier = _issue(idp)
        form = _code_form(code, verifier) | {"client_id": idp.client_id}
        with override_transport(idp.transport):
            assert (await _exchange(idp, form)).status_code == 200

    async def test_public_client_must_not_send_a_secret(self) -> None:
        idp = MockIdP(token_endpoint_auth_method="none", client_secret=None)
        code, verifier = _issue(idp)
        form = _code_form(code, verifier) | {"client_id": idp.client_id, "client_secret": "x"}
        with override_transport(idp.transport):
            assert (await _exchange(idp, form)).json()["error"] == "invalid_client"

    async def test_code_bound_to_another_client(self, mock_idp: MockIdP) -> None:
        code, verifier = _issue(mock_idp, client_id="other-client")
        resp = await _exchange(mock_idp, _code_form(code, verifier), auth=BASIC)
        assert resp.json()["error"] == "invalid_grant"

    async def test_issue_code_refuses_plain_pkce(self, mock_idp: MockIdP) -> None:
        with pytest.raises(ValueError):
            mock_idp.issue_code(
                nonce="n", code_challenge="abc", redirect_uri=REDIRECT_URI, code_challenge_method="plain"
            )

    async def test_bound_id_token_is_returned_verbatim(self, mock_idp: MockIdP) -> None:
        bad = mock_idp.id_token_variant("expired", nonce="n-123")
        code, verifier = _issue(mock_idp, id_token=bad)
        body = (await _exchange(mock_idp, _code_form(code, verifier), auth=BASIC)).json()
        assert body["id_token"] == bad


# ---------------------------------------------------------------------------
# Authorize, userinfo, end session
# ---------------------------------------------------------------------------


def _authorize_url(idp: MockIdP, challenge: str, **extra: str) -> str:
    params = {
        "response_type": "code",
        "client_id": idp.client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": "openid profile email",
        "state": "st-1",
        "nonce": "n-auth",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    } | extra
    return f"{idp.authorization_endpoint}?{urlencode(params)}"


class TestAuthorize:
    async def test_authorize_returns_callback_with_code_state_and_iss(self, mock_idp: MockIdP) -> None:
        verifier, challenge = pkce_pair()
        callback = mock_idp.authorize(_authorize_url(mock_idp, challenge), claims={"email": "a@example.com"})
        parts = urlsplit(callback)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == REDIRECT_URI
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert query["state"] == "st-1"
        assert query["iss"] == mock_idp.issuer
        resp = await _exchange(mock_idp, _code_form(query["code"], verifier), auth=BASIC)
        claims = verify_id_token(mock_idp, resp.json()["id_token"], nonce="n-auth")
        assert claims["email"] == "a@example.com"

    async def test_authorize_with_variant(self, mock_idp: MockIdP) -> None:
        verifier, challenge = pkce_pair()
        callback = mock_idp.authorize(_authorize_url(mock_idp, challenge), id_token_variant="wrong_aud")
        code = parse_qs(urlsplit(callback).query)["code"][0]
        token = (await _exchange(mock_idp, _code_form(code, verifier), auth=BASIC)).json()["id_token"]
        with pytest.raises(jwt.InvalidAudienceError):
            verify_id_token(mock_idp, token, nonce="n-auth")

    async def test_authorize_error(self, mock_idp: MockIdP) -> None:
        _, challenge = pkce_pair()
        callback = mock_idp.authorize(_authorize_url(mock_idp, challenge), error="access_denied")
        query = parse_qs(urlsplit(callback).query)
        assert query["error"] == ["access_denied"]
        assert query["state"] == ["st-1"]
        assert "code" not in query

    async def test_authorize_requires_s256(self, mock_idp: MockIdP) -> None:
        _, challenge = pkce_pair()
        with pytest.raises(ValueError):
            mock_idp.authorize(_authorize_url(mock_idp, challenge, code_challenge_method="plain"))

    async def test_authorize_refuses_unknown_client(self, mock_idp: MockIdP) -> None:
        _, challenge = pkce_pair()
        with pytest.raises(ValueError):
            mock_idp.authorize(_authorize_url(mock_idp, challenge, client_id="someone-else"))

    async def test_authorize_route_redirects(self, mock_idp: MockIdP) -> None:
        _, challenge = pkce_pair()
        transport_client = httpx.AsyncClient(transport=mock_idp.transport)
        async with transport_client:
            resp = await transport_client.get(_authorize_url(mock_idp, challenge))
        assert resp.status_code == 302
        assert resp.headers["Location"].startswith(REDIRECT_URI)
        assert mock_idp.calls["authorize"] == 1


class TestUserinfoAndEndSession:
    async def test_userinfo_returns_claims_for_the_access_token(self, mock_idp: MockIdP) -> None:
        code, verifier = _issue(mock_idp, claims={"email": "u@example.com"})
        access = (await _exchange(mock_idp, _code_form(code, verifier), auth=BASIC)).json()["access_token"]
        async with create_sso_client() as client:
            resp = await client.get(mock_idp.userinfo_endpoint, headers={"Authorization": f"Bearer {access}"})
        assert resp.status_code == 200
        assert resp.json()["sub"] == mock_idp.default_sub
        assert resp.json()["email"] == "u@example.com"

    async def test_userinfo_overrides(self, mock_idp: MockIdP) -> None:
        code, verifier = _issue(mock_idp)
        access = (await _exchange(mock_idp, _code_form(code, verifier), auth=BASIC)).json()["access_token"]
        mock_idp.userinfo_overrides["sub"] = "someone-else"
        async with create_sso_client() as client:
            resp = await client.get(mock_idp.userinfo_endpoint, headers={"Authorization": f"Bearer {access}"})
        assert resp.json()["sub"] == "someone-else"

    async def test_userinfo_rejects_unknown_token(self, mock_idp: MockIdP) -> None:
        async with create_sso_client() as client:
            resp = await client.get(mock_idp.userinfo_endpoint, headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401

    async def test_end_session_records_and_redirects(self, mock_idp: MockIdP) -> None:
        url = (
            mock_idp.end_session_endpoint
            + "?"
            + urlencode(
                {
                    "id_token_hint": "tok",
                    "client_id": mock_idp.client_id,
                    "post_logout_redirect_uri": "https://specivo.example.com/login/?logged_out=1",
                }
            )
        )
        async with httpx.AsyncClient(transport=mock_idp.transport) as client:
            resp = await client.get(url)
        assert resp.status_code == 302
        assert resp.headers["Location"] == "https://specivo.example.com/login/?logged_out=1"
        assert mock_idp.end_session_requests[0]["id_token_hint"] == "tok"


# ---------------------------------------------------------------------------
# Faults
# ---------------------------------------------------------------------------


class TestFaults:
    async def test_status_fault(self, mock_idp: MockIdP) -> None:
        mock_idp.set_fault("jwks", status=503)
        async with create_sso_client() as client:
            assert (await client.get(mock_idp.jwks_uri)).status_code == 503
            assert (await client.get(mock_idp.discovery_url)).status_code == 200

    async def test_timeout_fault(self, mock_idp: MockIdP) -> None:
        mock_idp.set_fault("token", timeout=True)
        async with create_sso_client() as client:
            with pytest.raises(httpx.TimeoutException):
                await client.post(mock_idp.token_endpoint, data={})

    async def test_oversized_fault_hits_the_client_cap(self, mock_idp: MockIdP) -> None:
        mock_idp.set_fault("jwks", body_size=2 * 1024 * 1024)
        async with create_sso_client() as client:
            with pytest.raises(SSOResponseTooLargeError):
                await client.get(mock_idp.jwks_uri)

    async def test_oversized_fault_size_is_exact(self, mock_idp: MockIdP) -> None:
        mock_idp.set_fault("jwks", body_size=1234)
        async with httpx.AsyncClient(transport=mock_idp.transport) as client:
            assert len((await client.get(mock_idp.jwks_uri)).content) == 1234

    async def test_cross_host_redirect_fault(self, mock_idp: MockIdP) -> None:
        mock_idp.set_fault("discovery", redirect_to="https://attacker.example.net/.well-known/openid-configuration")
        async with create_sso_client() as client:
            with pytest.raises(SSORedirectError):
                await client.get(mock_idp.discovery_url)

    async def test_clear_faults(self, mock_idp: MockIdP) -> None:
        mock_idp.set_fault("jwks", status=500)
        mock_idp.clear_faults()
        async with create_sso_client() as client:
            assert (await client.get(mock_idp.jwks_uri)).status_code == 200

    async def test_unknown_route_name_is_rejected(self, mock_idp: MockIdP) -> None:
        with pytest.raises(KeyError):
            mock_idp.set_fault("nope", status=500)

    async def test_faulted_requests_are_still_counted(self, mock_idp: MockIdP) -> None:
        mock_idp.set_fault("jwks", status=500)
        async with create_sso_client() as client:
            await client.get(mock_idp.jwks_uri)
        assert mock_idp.calls["jwks"] == 1


def test_whole_module_is_fast() -> None:
    """Minting must not generate keys per call (keys are cached per session)."""
    idp = MockIdP()
    start = time.perf_counter()
    for _ in range(50):
        idp.mint_id_token()
        idp.rotate()
    assert time.perf_counter() - start < 1.5
