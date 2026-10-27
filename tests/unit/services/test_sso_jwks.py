"""Unit tests for the JWKS cache (``specivo.services.sso.jwks``)."""

from __future__ import annotations

import json
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from jwt.algorithms import ECAlgorithm, OKPAlgorithm, RSAAlgorithm

from specivo.services.sso import jwks
from specivo.services.sso.discovery import parse_discovery
from specivo.services.sso.errors import (
    SsoDiscoveryInvalidError,
    SsoIdpUnreachableError,
    SsoTokenInvalidError,
    SsoUnknownSigningKeyError,
    TokenRejection,
)
from specivo.services.sso.jwks import (
    ALLOWED_ALGORITHMS,
    JWKS_MAX_TTL_SECONDS,
    JWKS_MIN_TTL_SECONDS,
    JWKS_REFETCH_INTERVAL_SECONDS,
    get_jwks,
    jwks_cache_key,
    refetch_jwks_for_unknown_kid,
    select_signing_key,
    signing_algorithms,
    ttl_from_cache_control,
    usable_signing_keys,
)
from specivo.testing.oidc_mock import MockIdP
from tests.unit.services.conftest import BrokenRedis, FakeRedis

pytestmark = pytest.mark.unit


def _rsa_jwk(**extra: Any) -> dict[str, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    return {**RSAAlgorithm.to_jwk(key, as_dict=True), **extra}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestTtl:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("max-age=300", 300),
            ("public, max-age=3600", 3600),
            ("max-age=10", JWKS_MIN_TTL_SECONDS),
            ("max-age=0", JWKS_MIN_TTL_SECONDS),
            ("max-age=999999", JWKS_MAX_TTL_SECONDS),
            ("no-store", JWKS_MIN_TTL_SECONDS),
            ("no-cache", JWKS_MIN_TTL_SECONDS),
            ("max-age=abc", JWKS_MIN_TTL_SECONDS),
            ('max-age="600"', 600),
            ("s-maxage=10, max-age=7200", 7200),
            (None, 3600),
            ("", 3600),
            ("private", 3600),
        ],
    )
    def test_clamped(self, header: str | None, expected: int) -> None:
        assert ttl_from_cache_control(header) == expected

    def test_bounds(self) -> None:
        assert JWKS_MIN_TTL_SECONDS == 300
        assert JWKS_MAX_TTL_SECONDS == 86400
        assert JWKS_REFETCH_INTERVAL_SECONDS == 60


class TestAlgorithms:
    def test_allowlist_never_contains_none_or_hmac(self) -> None:
        assert "none" not in ALLOWED_ALGORITHMS
        assert not any(alg.startswith("HS") for alg in ALLOWED_ALGORITHMS)
        assert {"RS256", "PS256", "ES256", "EdDSA"} <= ALLOWED_ALGORITHMS

    def test_intersection_with_discovery(self, mock_idp: MockIdP) -> None:
        mock_idp.discovery_overrides["id_token_signing_alg_values_supported"] = ["RS256", "HS256", "none", "PS512"]
        meta = parse_discovery(mock_idp.discovery_document(), expected_issuer=mock_idp.issuer)
        assert signing_algorithms(meta) == frozenset({"RS256", "PS512"})


class TestUsableKeys:
    def test_filters_encryption_and_symmetric_keys(self) -> None:
        sig = _rsa_jwk(kid="a", use="sig")
        no_use = _rsa_jwk(kid="b")
        enc = _rsa_jwk(kid="c", use="enc")
        oct_key = {"kty": "oct", "k": "c2VjcmV0", "kid": "d"}
        bad_alg = _rsa_jwk(kid="e", alg="RSA-OAEP")
        hs_alg = _rsa_jwk(kid="f", alg="HS256")
        ops_no_verify = _rsa_jwk(kid="g", key_ops=["encrypt"])
        ops_verify = _rsa_jwk(kid="h", key_ops=["verify"])
        keys = usable_signing_keys(
            {"keys": [sig, no_use, enc, oct_key, bad_alg, hs_alg, ops_no_verify, ops_verify, "junk", 7]}
        )
        assert [k["kid"] for k in keys] == ["a", "b", "h"]

    @pytest.mark.parametrize("doc", [{}, {"keys": "x"}, [], "keys", {"keys": [{"kty": "oct", "k": "AA"}]}])
    def test_no_usable_keys(self, doc: Any) -> None:
        assert usable_signing_keys(doc) == []


class TestSelectSigningKey:
    def test_by_kid(self, mock_idp: MockIdP) -> None:
        keys = usable_signing_keys(mock_idp.jwks())
        key = select_signing_key(keys, {"alg": "RS256", "kid": "rsa-1"}, frozenset({"RS256", "ES256"}))
        assert isinstance(key, jwt.PyJWK)
        assert key.key_id == "rsa-1"
        assert key.algorithm_name == "RS256"

    def test_ec_by_kid(self, mock_idp: MockIdP) -> None:
        keys = usable_signing_keys(mock_idp.jwks())
        key = select_signing_key(keys, {"alg": "ES256", "kid": "ec-1"}, frozenset({"RS256", "ES256"}))
        assert key.algorithm_name == "ES256"

    def test_unknown_kid(self, mock_idp: MockIdP) -> None:
        keys = usable_signing_keys(mock_idp.jwks())
        with pytest.raises(SsoUnknownSigningKeyError) as exc:
            select_signing_key(keys, {"alg": "RS256", "kid": "nope"}, frozenset({"RS256"}))
        assert exc.value.kid == "nope"
        assert exc.value.code == "sso_token_invalid"

    @pytest.mark.parametrize("alg", ["none", "HS256", "HS512", "RS1", "", None])
    def test_algorithm_not_allowed(self, mock_idp: MockIdP, alg: Any) -> None:
        keys = usable_signing_keys(mock_idp.jwks())
        with pytest.raises(SsoTokenInvalidError) as exc:
            select_signing_key(keys, {"alg": alg, "kid": "rsa-1"}, frozenset({"RS256", "HS256", "none"}))
        assert exc.value.reason == TokenRejection.ALG_NOT_ALLOWED

    def test_algorithm_not_in_discovery(self, mock_idp: MockIdP) -> None:
        keys = usable_signing_keys(mock_idp.jwks())
        with pytest.raises(SsoTokenInvalidError) as exc:
            select_signing_key(keys, {"alg": "ES256", "kid": "ec-1"}, frozenset({"RS256"}))
        assert exc.value.reason == TokenRejection.ALG_NOT_ALLOWED

    def test_alg_does_not_match_key(self, mock_idp: MockIdP) -> None:
        keys = usable_signing_keys(mock_idp.jwks())
        # The RSA key is published with alg=RS256.
        with pytest.raises(SsoTokenInvalidError) as exc:
            select_signing_key(keys, {"alg": "PS256", "kid": "rsa-1"}, frozenset({"RS256", "PS256"}))
        assert exc.value.reason == TokenRejection.KEY_MISMATCH

    def test_family_mismatch(self, mock_idp: MockIdP) -> None:
        keys = [{k: v for k, v in key.items() if k != "alg"} for key in usable_signing_keys(mock_idp.jwks())]
        with pytest.raises(SsoTokenInvalidError) as exc:
            select_signing_key(keys, {"alg": "ES256", "kid": "rsa-1"}, frozenset({"RS256", "ES256"}))
        assert exc.value.reason == TokenRejection.KEY_MISMATCH

    def test_curve_mismatch(self) -> None:
        public = ec.generate_private_key(ec.SECP384R1()).public_key()
        keys = [{**ECAlgorithm.to_jwk(public, as_dict=True), "kid": "p384"}]
        with pytest.raises(SsoTokenInvalidError) as exc:
            select_signing_key(keys, {"alg": "ES256", "kid": "p384"}, frozenset({"ES256", "ES384"}))
        assert exc.value.reason == TokenRejection.KEY_MISMATCH
        key = select_signing_key(keys, {"alg": "ES384", "kid": "p384"}, frozenset({"ES384"}))
        assert key.algorithm_name == "ES384"

    def test_ps256_with_rsa_key_without_alg(self) -> None:
        keys = [_rsa_jwk(kid="r")]
        key = select_signing_key(keys, {"alg": "PS256", "kid": "r"}, frozenset({"PS256"}))
        assert key.algorithm_name == "PS256"

    def test_eddsa(self) -> None:
        public = ed25519.Ed25519PrivateKey.generate().public_key()
        keys = [{**OKPAlgorithm.to_jwk(public, as_dict=True), "kid": "ed"}]
        key = select_signing_key(keys, {"alg": "EdDSA", "kid": "ed"}, frozenset({"EdDSA"}))
        assert key.algorithm_name == "EdDSA"
        with pytest.raises(SsoTokenInvalidError):
            select_signing_key(keys, {"alg": "RS256", "kid": "ed"}, frozenset({"RS256"}))

    def test_no_kid_with_single_matching_key(self, mock_idp: MockIdP) -> None:
        keys = usable_signing_keys(mock_idp.jwks())
        key = select_signing_key(keys, {"alg": "ES256"}, frozenset({"RS256", "ES256"}))
        assert key.key_id == "ec-1"

    def test_no_kid_ambiguous(self, mock_idp: MockIdP) -> None:
        mock_idp.rotate()
        keys = usable_signing_keys(mock_idp.jwks())
        with pytest.raises(SsoTokenInvalidError) as exc:
            select_signing_key(keys, {"alg": "RS256"}, frozenset({"RS256"}))
        assert exc.value.reason == TokenRejection.UNKNOWN_KID
        assert not isinstance(exc.value, SsoUnknownSigningKeyError)

    def test_unusable_key_material(self) -> None:
        keys = [{"kty": "RSA", "kid": "broken", "n": "AQAB", "e": "!!"}]
        with pytest.raises(SsoTokenInvalidError) as exc:
            select_signing_key(keys, {"alg": "RS256", "kid": "broken"}, frozenset({"RS256"}))
        assert exc.value.reason == TokenRejection.KEY_MISMATCH

    def test_non_string_kid(self, mock_idp: MockIdP) -> None:
        keys = usable_signing_keys(mock_idp.jwks())
        with pytest.raises(SsoTokenInvalidError) as exc:
            select_signing_key(keys, {"alg": "RS256", "kid": 5}, frozenset({"RS256"}))
        assert exc.value.reason == TokenRejection.MALFORMED


# ---------------------------------------------------------------------------
# Fetch, cache, refetch on unknown kid
# ---------------------------------------------------------------------------


class TestGetJwks:
    async def test_fetch_and_cache_with_header_ttl(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        mock_idp.jwks_cache_control = "max-age=7200"
        keys = await get_jwks(7, mock_idp.jwks_uri, redis=fake_redis)
        assert {k["kid"] for k in keys} == {"rsa-1", "ec-1"}
        assert jwks_cache_key(7) == "sso:jwks:7"
        assert fake_redis.ttl_of("sso:jwks:7") == pytest.approx(7200)
        assert mock_idp.calls["jwks"] == 1

        again = await get_jwks(7, mock_idp.jwks_uri, redis=fake_redis)
        assert again == keys
        assert mock_idp.calls["jwks"] == 1

    @pytest.mark.parametrize(
        ("header", "ttl"),
        [("max-age=5", JWKS_MIN_TTL_SECONDS), ("max-age=10000000", JWKS_MAX_TTL_SECONDS)],
    )
    async def test_cache_control_is_clamped(
        self, mock_idp: MockIdP, fake_redis: FakeRedis, header: str, ttl: int
    ) -> None:
        mock_idp.jwks_cache_control = header
        await get_jwks(7, mock_idp.jwks_uri, redis=fake_redis)
        assert fake_redis.ttl_of("sso:jwks:7") == pytest.approx(ttl)

    async def test_cache_expiry_refetches(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        await get_jwks(7, mock_idp.jwks_uri, redis=fake_redis)
        fake_redis.advance(301)
        await get_jwks(7, mock_idp.jwks_uri, redis=fake_redis)
        assert mock_idp.calls["jwks"] == 2

    async def test_enc_keys_are_ignored(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        enc = {**mock_idp.jwks()["keys"][0], "kid": "enc-1", "use": "enc"}
        original = mock_idp.jwks
        mock_idp.jwks = lambda: {"keys": [*original()["keys"], enc]}  # type: ignore[method-assign]
        keys = await get_jwks(7, mock_idp.jwks_uri, redis=fake_redis)
        assert "enc-1" not in {k["kid"] for k in keys}

    async def test_no_usable_keys(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        mock_idp.jwks = lambda: {"keys": [{"kty": "oct", "k": "AA", "kid": "x"}]}  # type: ignore[method-assign]
        with pytest.raises(SsoDiscoveryInvalidError) as exc:
            await get_jwks(7, mock_idp.jwks_uri, redis=fake_redis)
        assert exc.value.reason == "jwks_no_usable_keys"
        assert "sso:jwks:7" not in fake_redis.store

    async def test_non_json(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        mock_idp.set_fault("jwks", body_size=5)
        with pytest.raises(SsoDiscoveryInvalidError) as exc:
            await get_jwks(7, mock_idp.jwks_uri, redis=fake_redis)
        assert exc.value.reason == "jwks_not_json"

    @pytest.mark.parametrize(("fault", "reason"), [({"status": 503}, "http_503"), ({"timeout": True}, "timeout")])
    async def test_unreachable(
        self, mock_idp: MockIdP, fake_redis: FakeRedis, fault: dict[str, Any], reason: str
    ) -> None:
        mock_idp.set_fault("jwks", **fault)
        with pytest.raises(SsoIdpUnreachableError) as exc:
            await get_jwks(7, mock_idp.jwks_uri, redis=fake_redis)
        assert exc.value.reason == reason

    async def test_client_error_status_is_invalid(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        mock_idp.set_fault("jwks", status=404)
        with pytest.raises(SsoDiscoveryInvalidError) as exc:
            await get_jwks(7, mock_idp.jwks_uri, redis=fake_redis)
        assert exc.value.reason == "http_404"

    async def test_corrupt_cache_entry_refetches(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        await fake_redis.set("sso:jwks:7", "[not json", ex=600)
        keys = await get_jwks(7, mock_idp.jwks_uri, redis=fake_redis)
        assert keys
        assert mock_idp.calls["jwks"] == 1

    async def test_cache_for_another_uri_is_ignored(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        await fake_redis.set("sso:jwks:7", json.dumps({"uri": "https://old.example.com/certs", "keys": []}), ex=600)
        keys = await get_jwks(7, mock_idp.jwks_uri, redis=fake_redis)
        assert keys
        assert mock_idp.calls["jwks"] == 1

    async def test_cached_entry_without_usable_keys_refetches(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        entry = {"uri": mock_idp.jwks_uri, "keys": [{"kty": "oct", "k": "AA", "kid": "x"}]}
        await fake_redis.set("sso:jwks:7", json.dumps(entry), ex=600)
        keys = await get_jwks(7, mock_idp.jwks_uri, redis=fake_redis)
        assert {k["kid"] for k in keys} == {"rsa-1", "ec-1"}
        assert mock_idp.calls["jwks"] == 1

    async def test_redis_down_fetches(self, mock_idp: MockIdP, broken_redis: BrokenRedis) -> None:
        keys = await get_jwks(7, mock_idp.jwks_uri, redis=broken_redis)  # type: ignore[arg-type]
        assert keys
        assert mock_idp.calls["jwks"] == 1

    async def test_default_redis_is_the_shared_client(
        self, mock_idp: MockIdP, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _get_redis() -> FakeRedis:
            return fake_redis

        monkeypatch.setattr(jwks, "get_redis", _get_redis)
        await get_jwks(7, mock_idp.jwks_uri)
        assert "sso:jwks:7" in fake_redis.store


class TestRefetchOnUnknownKid:
    async def test_rotation_one_refetch_then_success(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        keys = await get_jwks(7, mock_idp.jwks_uri, redis=fake_redis)
        new_kid = mock_idp.rotate()
        assert new_kid not in {k["kid"] for k in keys}

        refreshed = await refetch_jwks_for_unknown_kid(7, mock_idp.jwks_uri, redis=fake_redis)
        assert refreshed is not None
        assert new_kid in {k["kid"] for k in refreshed}
        assert mock_idp.calls["jwks"] == 2
        # The cache now serves the new set.
        assert new_kid in {k["kid"] for k in await get_jwks(7, mock_idp.jwks_uri, redis=fake_redis)}
        assert mock_idp.calls["jwks"] == 2

    async def test_second_unknown_kid_within_interval_is_not_refetched(
        self, mock_idp: MockIdP, fake_redis: FakeRedis
    ) -> None:
        await get_jwks(7, mock_idp.jwks_uri, redis=fake_redis)
        assert await refetch_jwks_for_unknown_kid(7, mock_idp.jwks_uri, redis=fake_redis) is not None
        assert await refetch_jwks_for_unknown_kid(7, mock_idp.jwks_uri, redis=fake_redis) is None
        assert mock_idp.calls["jwks"] == 2

        fake_redis.advance(JWKS_REFETCH_INTERVAL_SECONDS + 1)
        assert await refetch_jwks_for_unknown_kid(7, mock_idp.jwks_uri, redis=fake_redis) is not None
        assert mock_idp.calls["jwks"] == 3

    async def test_rate_limit_is_per_provider(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        assert await refetch_jwks_for_unknown_kid(7, mock_idp.jwks_uri, redis=fake_redis) is not None
        assert await refetch_jwks_for_unknown_kid(8, mock_idp.jwks_uri, redis=fake_redis) is not None
        assert mock_idp.calls["jwks"] == 2

    async def test_rate_limit_without_redis_uses_process_memory(
        self, mock_idp: MockIdP, broken_redis: BrokenRedis
    ) -> None:
        jwks._local_refetch_at.pop(4242, None)
        assert await refetch_jwks_for_unknown_kid(4242, mock_idp.jwks_uri, redis=broken_redis) is not None  # type: ignore[arg-type]
        assert await refetch_jwks_for_unknown_kid(4242, mock_idp.jwks_uri, redis=broken_redis) is None  # type: ignore[arg-type]
        assert mock_idp.calls["jwks"] == 1
        jwks._local_refetch_at[4242] -= JWKS_REFETCH_INTERVAL_SECONDS + 1
        assert await refetch_jwks_for_unknown_kid(4242, mock_idp.jwks_uri, redis=broken_redis) is not None  # type: ignore[arg-type]
        jwks._local_refetch_at.pop(4242, None)


async def test_cache_and_refetch_lock_with_real_redis(mock_idp: MockIdP) -> None:
    """The same flow against the real Redis client (``REDIS_URL``), not the fake."""
    import os
    import secrets

    import redis.asyncio as aioredis

    client = aioredis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    provider_id = 800_000_000 + secrets.randbelow(100_000_000)
    keys = [jwks_cache_key(provider_id), f"sso:jwks:refetch:{provider_id}"]
    try:
        await get_jwks(provider_id, mock_idp.jwks_uri, redis=client)
        await get_jwks(provider_id, mock_idp.jwks_uri, redis=client)
        assert mock_idp.calls["jwks"] == 1
        assert 0 < await client.ttl(keys[0]) <= 300

        assert await refetch_jwks_for_unknown_kid(provider_id, mock_idp.jwks_uri, redis=client) is not None
        assert await refetch_jwks_for_unknown_kid(provider_id, mock_idp.jwks_uri, redis=client) is None
        assert 0 < await client.ttl(keys[1]) <= JWKS_REFETCH_INTERVAL_SECONDS
        assert mock_idp.calls["jwks"] == 2
    finally:
        await client.delete(*keys)
        await client.aclose()
