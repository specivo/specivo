"""Secrets never reach the logs.

Runs a full exchange (discovery, token, JWKS, ID token, userinfo) plus the
failure paths at DEBUG level for every logger, then greps every captured
record for the client secret, the code, the PKCE verifier and the raw tokens.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from specivo.services.sso.discovery import get_provider_metadata
from specivo.services.sso.errors import SsoError
from specivo.services.sso.id_token import verify_id_token, verify_logout_token
from specivo.services.sso.log import REDACTED, RedactTokensFilter, logger, redact, sanitize_idp_text
from specivo.services.sso.tokens import exchange_code, fetch_userinfo
from specivo.testing.oidc_mock import MockIdP, override_transport, pkce_pair
from tests.unit.services.conftest import FakeRedis, make_provider

pytestmark = pytest.mark.unit

REDIRECT_URI = "https://specivo.example.com/auth/oidc/test-idp/callback/"


def _all_text(caplog: pytest.LogCaptureFixture) -> str:
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(repr(record.args))
        parts.append(repr(record.__dict__))
    return "\n".join(parts) + caplog.text


async def test_no_secrets_in_logs(mock_idp: MockIdP, fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    provider = make_provider(mock_idp)
    verifier, challenge = pkce_pair()
    code = mock_idp.issue_code(nonce=mock_idp.default_nonce, code_challenge=challenge, redirect_uri=REDIRECT_URI)

    meta = await get_provider_metadata(provider, redis=fake_redis)
    tokens = await exchange_code(provider, meta, code=code, redirect_uri=REDIRECT_URI, code_verifier=verifier)
    await verify_id_token(
        tokens.id_token,
        provider=provider,
        metadata=meta,
        nonce=mock_idp.default_nonce,
        access_token=tokens.access_token,
        redis=fake_redis,
    )
    await fetch_userinfo(meta, tokens.access_token, expected_sub=mock_idp.default_sub)

    # Failure paths log too.
    secrets_seen = [mock_idp.client_secret or "", code, verifier, tokens.id_token, tokens.access_token]
    with pytest.raises(SsoError):
        await exchange_code(provider, meta, code=code, redirect_uri=REDIRECT_URI, code_verifier=verifier)
    bad_tokens = [
        mock_idp.id_token_variant(name) for name in ("wrong_nonce", "unknown_kid", "tampered", "expired", "alg_none")
    ]
    for raw in bad_tokens:
        with pytest.raises(SsoError):
            await verify_id_token(raw, provider=provider, metadata=meta, nonce=mock_idp.default_nonce, redis=fake_redis)
    logout = mock_idp.logout_token_variant("with_nonce")
    with pytest.raises(SsoError):
        await verify_logout_token(logout, provider=provider, metadata=meta, redis=fake_redis)
    mock_idp.userinfo_overrides["sub"] = "other"
    with pytest.raises(SsoError):
        await fetch_userinfo(meta, tokens.access_token, expected_sub=mock_idp.default_sub)

    def leaky(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": f"bad code {code} token {tokens.id_token}"}
        )

    with override_transport(httpx.MockTransport(leaky)), pytest.raises(SsoError):
        await exchange_code(provider, meta, code=code, redirect_uri=REDIRECT_URI, code_verifier=verifier)

    assert caplog.records, "the SSO layer should log its failures"
    text = _all_text(caplog)
    for value in [*secrets_seen, *bad_tokens, logout, mock_idp.default_nonce]:
        assert value and value not in text


def test_redact() -> None:
    raw = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln"
    assert redact(f"token={raw} end") == f"token={REDACTED} end"
    assert redact("no tokens here") == "no tokens here"


def test_filter_rewrites_record() -> None:
    raw = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln"
    record = logging.LogRecord("specivo.sso", logging.INFO, __file__, 1, "value %s", (raw,), None)
    assert RedactTokensFilter().filter(record)
    assert record.getMessage() == f"value {REDACTED}"
    assert any(isinstance(f, RedactTokensFilter) for f in logger.filters)


def test_sanitize_idp_text() -> None:
    assert sanitize_idp_text("a\nb\x00c") == "a?b?c"
    assert len(sanitize_idp_text("x" * 500)) == 200
    assert sanitize_idp_text(5) == "5"
