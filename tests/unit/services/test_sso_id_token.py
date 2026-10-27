"""Unit tests for ID token and logout token validation (``specivo.services.sso.id_token``).

Every negative variant the mock IdP can mint is rejected, each for its own
reason; the reasons are asserted so a variant cannot pass for the wrong cause
(for example a tampered token failing only because its nonce is missing).
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

import jwt
import pytest

from specivo.services.sso.discovery import ProviderMetadata, parse_discovery
from specivo.services.sso.errors import SsoTokenInvalidError, SsoUnknownSigningKeyError, TokenRejection
from specivo.services.sso.id_token import (
    MAX_TOKEN_AGE_SECONDS,
    IdTokenClaims,
    LogoutTokenClaims,
    validate_id_token,
    validate_logout_token,
    verify_id_token,
    verify_logout_token,
)
from specivo.services.sso.jwks import signing_algorithms, usable_signing_keys
from specivo.testing.oidc_mock import (
    BACKCHANNEL_LOGOUT_EVENT,
    ID_TOKEN_VARIANTS,
    LOGOUT_TOKEN_VARIANTS,
    OMIT,
    MockIdP,
    at_hash,
)
from tests.unit.services.conftest import FakeRedis, make_provider

pytestmark = pytest.mark.unit

LEEWAY = 60

#: Expected rejection reason for every broken ID token variant.
ID_VARIANT_REASONS: dict[str, TokenRejection] = {
    "alg_none": TokenRejection.ALG_NOT_ALLOWED,
    "hs256_with_public_key": TokenRejection.ALG_NOT_ALLOWED,
    "unknown_kid": TokenRejection.UNKNOWN_KID,
    "rotated_key": TokenRejection.UNKNOWN_KID,
    "wrong_signing_key": TokenRejection.BAD_SIGNATURE,
    "tampered": TokenRejection.BAD_SIGNATURE,
    "expired": TokenRejection.EXPIRED,
    "future_iat": TokenRejection.ISSUED_IN_FUTURE,
    "wrong_iss": TokenRejection.WRONG_ISSUER,
    "wrong_aud": TokenRejection.WRONG_AUDIENCE,
    "wrong_azp": TokenRejection.WRONG_AZP,
    "multi_aud_without_azp": TokenRejection.WRONG_AZP,
    "missing_sub": TokenRejection.MISSING_CLAIM,
    "missing_nonce": TokenRejection.MISSING_CLAIM,
    "wrong_nonce": TokenRejection.NONCE_MISMATCH,
}

LOGOUT_VARIANT_REASONS: dict[str, TokenRejection] = {
    **{k: v for k, v in ID_VARIANT_REASONS.items() if k in LOGOUT_TOKEN_VARIANTS},
    "missing_jti": TokenRejection.MISSING_CLAIM,
    "missing_events": TokenRejection.MISSING_EVENTS,
    "missing_sid_and_sub": TokenRejection.MISSING_SID_AND_SUB,
    "with_nonce": TokenRejection.NONCE_PRESENT,
    "wrong_typ": TokenRejection.WRONG_TYP,
}


def test_every_mock_variant_has_an_expected_reason() -> None:
    assert set(ID_VARIANT_REASONS) == set(ID_TOKEN_VARIANTS) - {"groups_overage"}
    assert set(LOGOUT_VARIANT_REASONS) == set(LOGOUT_TOKEN_VARIANTS)


def _meta(idp: MockIdP) -> ProviderMetadata:
    return parse_discovery(idp.discovery_document(), expected_issuer=idp.issuer)


def _validate(idp: MockIdP, raw: str, **kwargs: Any) -> IdTokenClaims:
    params: dict[str, Any] = {
        "issuer": idp.issuer,
        "client_id": idp.client_id,
        "keys": usable_signing_keys(idp.jwks()),
        "algorithms": signing_algorithms(_meta(idp)),
        "nonce": idp.default_nonce,
        "leeway": LEEWAY,
    }
    params.update(kwargs)
    return validate_id_token(raw, **params)


def _validate_logout(idp: MockIdP, raw: str, **kwargs: Any) -> LogoutTokenClaims:
    params: dict[str, Any] = {
        "issuer": idp.issuer,
        "client_id": idp.client_id,
        "keys": usable_signing_keys(idp.jwks()),
        "algorithms": signing_algorithms(_meta(idp)),
        "leeway": LEEWAY,
    }
    params.update(kwargs)
    return validate_logout_token(raw, **params)


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _sign_rs256(idp: MockIdP, claims: dict[str, Any], **headers: Any) -> str:
    key = idp._active_key("RS256")
    return jwt.encode(claims, key.private_key, algorithm="RS256", headers={"kid": key.kid, **headers})


# ---------------------------------------------------------------------------
# ID token
# ---------------------------------------------------------------------------


class TestValidIdToken:
    def test_valid_rs256(self, mock_idp: MockIdP) -> None:
        claims = _validate(mock_idp, mock_idp.mint_id_token())
        assert claims.subject == mock_idp.default_sub
        assert claims.issuer == mock_idp.issuer
        assert claims.audience == (mock_idp.client_id,)
        assert claims.sid == mock_idp.default_sid
        assert claims.nonce == mock_idp.default_nonce
        assert claims.claims["email"] == "member@example.com"
        assert claims.auth_time is not None

    def test_valid_es256(self, mock_idp: MockIdP) -> None:
        claims = _validate(mock_idp, mock_idp.mint_id_token(alg="ES256"))
        assert claims.subject == mock_idp.default_sub

    def test_groups_overage_variant_is_validly_signed(self, mock_idp: MockIdP) -> None:
        claims = _validate(mock_idp, mock_idp.id_token_variant("groups_overage"))
        assert "_claim_names" in claims.claims

    def test_multi_aud_with_matching_azp(self, mock_idp: MockIdP) -> None:
        raw = mock_idp.mint_id_token(aud=[mock_idp.client_id, "other"], azp=mock_idp.client_id)
        claims = _validate(mock_idp, raw)
        assert claims.audience == (mock_idp.client_id, "other")
        assert claims.azp == mock_idp.client_id

    def test_single_aud_with_matching_azp(self, mock_idp: MockIdP) -> None:
        _validate(mock_idp, mock_idp.mint_id_token(azp=mock_idp.client_id))

    def test_single_aud_list(self, mock_idp: MockIdP) -> None:
        claims = _validate(mock_idp, mock_idp.mint_id_token(aud=[mock_idp.client_id]))
        assert claims.audience == (mock_idp.client_id,)

    def test_expired_within_leeway_is_accepted(self, mock_idp: MockIdP) -> None:
        now = int(time.time())
        _validate(mock_idp, mock_idp.mint_id_token(iat=now - 120, exp=now - 30))

    def test_iat_slightly_in_future_within_leeway(self, mock_idp: MockIdP) -> None:
        now = int(time.time())
        _validate(mock_idp, mock_idp.mint_id_token(iat=now + 30, exp=now + 300))

    def test_at_hash_matches(self, mock_idp: MockIdP) -> None:
        raw = mock_idp.mint_id_token(at_hash=at_hash("the-access-token"))
        _validate(mock_idp, raw, access_token="the-access-token")

    def test_at_hash_ignored_without_access_token(self, mock_idp: MockIdP) -> None:
        _validate(mock_idp, mock_idp.mint_id_token(at_hash="whatever"))

    def test_at_hash_es384_family(self) -> None:
        # at_hash uses the hash of the signing algorithm: SHA-384 for *384.
        from specivo.services.sso.id_token import compute_at_hash

        token = "abc"
        expected = base64.urlsafe_b64encode(hashlib.sha384(b"abc").digest()[:24]).rstrip(b"=").decode()
        assert compute_at_hash(token, "ES384") == expected
        assert compute_at_hash(token, "PS512") == (
            base64.urlsafe_b64encode(hashlib.sha512(b"abc").digest()[:32]).rstrip(b"=").decode()
        )
        assert compute_at_hash(token, "EdDSA") == compute_at_hash(token, "RS512")
        assert compute_at_hash(token, "RS256") == at_hash(token)

    def test_now_and_leeway_parameters(self, mock_idp: MockIdP) -> None:
        raw = mock_idp.mint_id_token()
        later = time.time() + mock_idp.token_ttl_seconds + 10
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, raw, now=later, leeway=0)
        assert exc.value.reason == TokenRejection.EXPIRED

    def test_default_leeway_from_settings(self, mock_idp: MockIdP) -> None:
        now = int(time.time())
        raw = mock_idp.mint_id_token(iat=now - 100, exp=now - 30)
        _validate(mock_idp, raw, leeway=None)  # settings default is 60 s

    def test_repr_hides_claims(self, mock_idp: MockIdP) -> None:
        claims = _validate(mock_idp, mock_idp.mint_id_token())
        assert "member@example.com" not in repr(claims)


class TestIdTokenVariants:
    @pytest.mark.parametrize("variant", sorted(ID_VARIANT_REASONS))
    def test_variant_rejected_for_the_right_reason(self, mock_idp: MockIdP, variant: str) -> None:
        raw = mock_idp.id_token_variant(variant)
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, raw)
        assert exc.value.code == "sso_token_invalid"
        assert exc.value.reason == ID_VARIANT_REASONS[variant], exc.value.detail

    def test_missing_sub_names_the_claim(self, mock_idp: MockIdP) -> None:
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, mock_idp.id_token_variant("missing_sub"))
        assert exc.value.detail == "sub"

    def test_unknown_kid_is_the_refetchable_subclass(self, mock_idp: MockIdP) -> None:
        with pytest.raises(SsoUnknownSigningKeyError):
            _validate(mock_idp, mock_idp.id_token_variant("unknown_kid"))


class TestIdTokenChecks:
    def test_alg_not_in_discovery_list(self, mock_idp: MockIdP) -> None:
        mock_idp.discovery_overrides["id_token_signing_alg_values_supported"] = ["RS256"]
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, mock_idp.mint_id_token(alg="ES256"))
        assert exc.value.reason == TokenRejection.ALG_NOT_ALLOWED

    def test_hs256_rejected_even_if_caller_allows_it(self, mock_idp: MockIdP) -> None:
        raw = mock_idp.id_token_variant("hs256_with_public_key")
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, raw, algorithms=frozenset({"HS256", "RS256"}))
        assert exc.value.reason == TokenRejection.ALG_NOT_ALLOWED

    def test_expired_beyond_leeway(self, mock_idp: MockIdP) -> None:
        now = int(time.time())
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, mock_idp.mint_id_token(iat=now - 200, exp=now - LEEWAY - 5))
        assert exc.value.reason == TokenRejection.EXPIRED

    def test_iat_older_than_ten_minutes(self, mock_idp: MockIdP) -> None:
        now = int(time.time())
        raw = mock_idp.mint_id_token(iat=now - MAX_TOKEN_AGE_SECONDS - LEEWAY - 5, exp=now + 300)
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, raw)
        assert exc.value.reason == TokenRejection.TOO_OLD
        assert MAX_TOKEN_AGE_SECONDS == 600

    def test_nbf_in_future(self, mock_idp: MockIdP) -> None:
        now = int(time.time())
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, mock_idp.mint_id_token(nbf=now + 600))
        assert exc.value.reason == TokenRejection.NOT_YET_VALID

    def test_nbf_in_past_ok(self, mock_idp: MockIdP) -> None:
        _validate(mock_idp, mock_idp.mint_id_token(nbf=int(time.time()) - 5))

    @pytest.mark.parametrize("claim", ["iss", "aud", "exp", "iat"])
    def test_required_claims(self, mock_idp: MockIdP, claim: str) -> None:
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, mock_idp.mint_id_token(**{claim: OMIT}))
        assert exc.value.reason == TokenRejection.MISSING_CLAIM
        assert exc.value.detail == claim

    @pytest.mark.parametrize(
        ("claim", "value"),
        [("exp", "soon"), ("iat", True), ("nbf", "x"), ("exp", 1.5e30), ("auth_time", "x")],
    )
    def test_non_numeric_times(self, mock_idp: MockIdP, claim: str, value: Any) -> None:
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, mock_idp.mint_id_token(**{claim: value}))
        assert exc.value.reason == TokenRejection.MALFORMED

    @pytest.mark.parametrize("sub", ["", 42, "x" * 256])
    def test_bad_sub(self, mock_idp: MockIdP, sub: Any) -> None:
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, mock_idp.mint_id_token(sub=sub))
        assert exc.value.reason in (TokenRejection.MALFORMED, TokenRejection.MISSING_CLAIM)

    def test_aud_wrong_type(self, mock_idp: MockIdP) -> None:
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, mock_idp.mint_id_token(aud={"x": 1}))
        assert exc.value.reason == TokenRejection.WRONG_AUDIENCE

    def test_aud_list_with_non_string(self, mock_idp: MockIdP) -> None:
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, mock_idp.mint_id_token(aud=[mock_idp.client_id, 5], azp=mock_idp.client_id))
        assert exc.value.reason == TokenRejection.WRONG_AUDIENCE

    def test_iss_not_a_string(self, mock_idp: MockIdP) -> None:
        # PyJWT refuses to encode a non-string iss, so sign the payload bytes directly.
        key = mock_idp._active_key("RS256")
        payload = json.dumps(mock_idp.id_token_claims(iss=["x"])).encode()
        raw = jwt.api_jws.encode(payload, key.private_key, algorithm="RS256", headers={"kid": key.kid})
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, raw)
        assert exc.value.reason == TokenRejection.WRONG_ISSUER

    def test_nonce_not_a_string(self, mock_idp: MockIdP) -> None:
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, mock_idp.mint_id_token(nonce=12345))
        assert exc.value.reason == TokenRejection.NONCE_MISMATCH

    def test_non_ascii_nonce(self, mock_idp: MockIdP) -> None:
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, mock_idp.mint_id_token(nonce="ไทย"), nonce="abc")
        assert exc.value.reason == TokenRejection.NONCE_MISMATCH

    def test_bad_at_hash(self, mock_idp: MockIdP) -> None:
        raw = mock_idp.mint_id_token(at_hash=at_hash("another-token"))
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, raw, access_token="the-access-token")
        assert exc.value.reason == TokenRejection.AT_HASH_MISMATCH

    def test_at_hash_not_a_string(self, mock_idp: MockIdP) -> None:
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, mock_idp.mint_id_token(at_hash=5), access_token="t")
        assert exc.value.reason == TokenRejection.AT_HASH_MISMATCH

    def test_logout_token_is_not_an_id_token(self, mock_idp: MockIdP) -> None:
        raw = mock_idp.mint_logout_token(nonce=mock_idp.default_nonce)
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, raw)
        assert exc.value.reason == TokenRejection.WRONG_TYP

    @pytest.mark.parametrize("raw", ["", "abc", "a.b", "a.b.c", "eyJ.eyJ.", 123])
    def test_malformed(self, mock_idp: MockIdP, raw: Any) -> None:
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, raw)
        assert exc.value.reason == TokenRejection.MALFORMED

    def test_crit_header_refused(self, mock_idp: MockIdP) -> None:
        raw = _sign_rs256(mock_idp, mock_idp.id_token_claims(), crit=["exp"])
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, raw)
        assert exc.value.reason == TokenRejection.MALFORMED

    def test_payload_not_an_object(self, mock_idp: MockIdP) -> None:
        key = mock_idp._active_key("RS256")
        raw = jwt.api_jws.encode(b"[1,2]", key.private_key, algorithm="RS256", headers={"kid": key.kid})
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, raw)
        assert exc.value.reason == TokenRejection.MALFORMED

    @pytest.mark.filterwarnings("ignore:The RSA key is 1024 bits long")
    def test_rsa_key_shorter_than_2048_bits_refused(self, mock_idp: MockIdP) -> None:
        from cryptography.hazmat.primitives.asymmetric import rsa
        from jwt.algorithms import RSAAlgorithm

        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        jwk = {**RSAAlgorithm.to_jwk(weak.public_key(), as_dict=True), "kid": "weak", "alg": "RS256"}
        raw = jwt.encode(mock_idp.id_token_claims(), weak, algorithm="RS256", headers={"kid": "weak"})
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, raw, keys=[jwk])
        assert exc.value.reason == TokenRejection.KEY_MISMATCH

    def test_raw_token_never_in_error(self, mock_idp: MockIdP) -> None:
        raw = mock_idp.id_token_variant("wrong_nonce")
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate(mock_idp, raw)
        text = f"{exc.value} {exc.value.reason} {exc.value.detail} {exc.value!r}"
        assert raw not in text
        assert mock_idp.default_nonce not in text


# ---------------------------------------------------------------------------
# Logout token
# ---------------------------------------------------------------------------


class TestLogoutToken:
    def test_valid_with_sid_and_sub(self, mock_idp: MockIdP) -> None:
        claims = _validate_logout(mock_idp, mock_idp.mint_logout_token())
        assert claims.sid == mock_idp.default_sid
        assert claims.subject == mock_idp.default_sub
        assert claims.jti

    def test_valid_with_sid_only(self, mock_idp: MockIdP) -> None:
        claims = _validate_logout(mock_idp, mock_idp.mint_logout_token(sub=OMIT))
        assert claims.subject is None
        assert claims.sid == mock_idp.default_sid

    def test_valid_with_sub_only(self, mock_idp: MockIdP) -> None:
        claims = _validate_logout(mock_idp, mock_idp.mint_logout_token(sid=OMIT))
        assert claims.sid is None
        assert claims.subject == mock_idp.default_sub

    def test_valid_es256(self, mock_idp: MockIdP) -> None:
        _validate_logout(mock_idp, mock_idp.mint_logout_token(alg="ES256"))

    def test_typ_absent_is_accepted(self, mock_idp: MockIdP) -> None:
        claims = mock_idp.logout_token_claims()
        key = mock_idp._active_key("RS256")
        raw = jwt.encode(claims, key.private_key, algorithm="RS256", headers={"kid": key.kid, "typ": None})
        header = jwt.get_unverified_header(raw)
        assert "typ" not in header
        _validate_logout(mock_idp, raw)

    def test_media_type_typ_is_accepted(self, mock_idp: MockIdP) -> None:
        raw = _sign_rs256(mock_idp, mock_idp.logout_token_claims(), typ="application/logout+jwt")
        _validate_logout(mock_idp, raw)

    @pytest.mark.parametrize("variant", sorted(LOGOUT_VARIANT_REASONS))
    def test_variant_rejected_for_the_right_reason(self, mock_idp: MockIdP, variant: str) -> None:
        raw = mock_idp.logout_token_variant(variant)
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate_logout(mock_idp, raw)
        assert exc.value.reason == LOGOUT_VARIANT_REASONS[variant], exc.value.detail

    @pytest.mark.parametrize(
        "events",
        [
            {"http://schemas.openid.net/event/other": {}},
            {BACKCHANNEL_LOGOUT_EVENT: "yes"},
            [BACKCHANNEL_LOGOUT_EVENT],
        ],
    )
    def test_bad_events(self, mock_idp: MockIdP, events: Any) -> None:
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate_logout(mock_idp, mock_idp.mint_logout_token(events=events))
        assert exc.value.reason == TokenRejection.MISSING_EVENTS

    @pytest.mark.parametrize("claim", ["iat", "exp", "aud", "iss"])
    def test_required_claims(self, mock_idp: MockIdP, claim: str) -> None:
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate_logout(mock_idp, mock_idp.mint_logout_token(**{claim: OMIT}))
        assert exc.value.reason == TokenRejection.MISSING_CLAIM

    def test_too_old(self, mock_idp: MockIdP) -> None:
        now = int(time.time())
        raw = mock_idp.mint_logout_token(iat=now - MAX_TOKEN_AGE_SECONDS - LEEWAY - 5, exp=now + 60)
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate_logout(mock_idp, raw)
        assert exc.value.reason == TokenRejection.TOO_OLD

    @pytest.mark.parametrize(("claim", "value"), [("jti", 5), ("jti", ""), ("sid", 5), ("sub", "")])
    def test_bad_identifier_types(self, mock_idp: MockIdP, claim: str, value: Any) -> None:
        with pytest.raises(SsoTokenInvalidError):
            _validate_logout(mock_idp, mock_idp.mint_logout_token(**{claim: value}))

    def test_id_token_is_not_a_logout_token(self, mock_idp: MockIdP) -> None:
        with pytest.raises(SsoTokenInvalidError) as exc:
            _validate_logout(mock_idp, mock_idp.mint_id_token())
        # ID tokens carry typ=JWT and a nonce.
        assert exc.value.reason in (TokenRejection.WRONG_TYP, TokenRejection.NONCE_PRESENT)


# ---------------------------------------------------------------------------
# Async glue: JWKS lookup + refetch on unknown kid
# ---------------------------------------------------------------------------


class TestVerifyWithJwks:
    async def test_verify_id_token(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        provider = make_provider(mock_idp)
        meta = _meta(mock_idp)
        token = "access-token-value"
        raw = mock_idp.mint_id_token(at_hash=at_hash(token))
        claims = await verify_id_token(
            raw, provider=provider, metadata=meta, nonce=mock_idp.default_nonce, access_token=token, redis=fake_redis
        )
        assert claims.subject == mock_idp.default_sub

    async def test_key_rotation(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        provider = make_provider(mock_idp)
        meta = _meta(mock_idp)
        await verify_id_token(
            mock_idp.mint_id_token(), provider=provider, metadata=meta, nonce=mock_idp.default_nonce, redis=fake_redis
        )
        assert mock_idp.calls["jwks"] == 1

        mock_idp.rotate(drop_old=True)
        claims = await verify_id_token(
            mock_idp.mint_id_token(), provider=provider, metadata=meta, nonce=mock_idp.default_nonce, redis=fake_redis
        )
        assert claims.subject == mock_idp.default_sub
        assert mock_idp.calls["jwks"] == 2

    async def test_old_key_rejected_after_rotation_with_drop(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        provider = make_provider(mock_idp)
        meta = _meta(mock_idp)
        old_token = mock_idp.mint_id_token()
        mock_idp.rotate(drop_old=True)
        with pytest.raises(SsoTokenInvalidError) as exc:
            await verify_id_token(
                old_token, provider=provider, metadata=meta, nonce=mock_idp.default_nonce, redis=fake_redis
            )
        assert exc.value.reason == TokenRejection.UNKNOWN_KID

    async def test_kid_spam_is_rate_limited(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        provider = make_provider(mock_idp)
        meta = _meta(mock_idp)
        for _ in range(5):
            with pytest.raises(SsoUnknownSigningKeyError):
                await verify_id_token(
                    mock_idp.id_token_variant("unknown_kid"),
                    provider=provider,
                    metadata=meta,
                    nonce=mock_idp.default_nonce,
                    redis=fake_redis,
                )
        # One initial fetch plus a single refetch, however many unknown kids arrive.
        assert mock_idp.calls["jwks"] == 2

    async def test_verify_logout_token(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        provider = make_provider(mock_idp)
        claims = await verify_logout_token(
            mock_idp.mint_logout_token(), provider=provider, metadata=_meta(mock_idp), redis=fake_redis
        )
        assert claims.sid == mock_idp.default_sid

    async def test_verify_logout_token_after_rotation(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        provider = make_provider(mock_idp)
        meta = _meta(mock_idp)
        await verify_logout_token(mock_idp.mint_logout_token(), provider=provider, metadata=meta, redis=fake_redis)
        mock_idp.rotate(family="EC", drop_old=True)
        await verify_logout_token(
            mock_idp.mint_logout_token(alg="ES256"), provider=provider, metadata=meta, redis=fake_redis
        )
        assert mock_idp.calls["jwks"] == 2
