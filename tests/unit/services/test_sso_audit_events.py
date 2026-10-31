"""SSO account and configuration events: one log line and one audit event each.

Every function in :mod:`specivo.services.sso.events` writes a ``specivo.sso``
log line (in every edition) and records an ``AuditEvent`` into the audit
buffer bound to the current request. The buffer only receives the event when
the enterprise ``security_audit_log`` feature is registered; the log line is
there either way. Details carry the provider slug, the issuer, the hashed
subject, ids and counts, never the raw subject.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from specivo.services.security_audit_service import (
    AuditEvent,
    SecurityAuditService,
    bind_audit_context,
    current_request_id,
    record_event,
    reset_audit_context,
)
from specivo.services.sso import events
from specivo.services.sso.events import subject_hash
from specivo.services.sso.log import REDACTED, redact

pytestmark = pytest.mark.unit

SUB = "raw-subject-0f3c9a"
ISS = "https://idp.example.com/realms/test"


@pytest.fixture
def audit_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SecurityAuditService, "_audit_enabled", staticmethod(lambda: True))


@pytest.fixture
def audit_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SecurityAuditService, "_audit_enabled", staticmethod(lambda: False))


@pytest.fixture
def buffer() -> Iterator[list[dict[str, Any]]]:
    events_buffer: list[dict[str, Any]] = []
    token = bind_audit_context(
        events_buffer, ip_address="203.0.113.7", user_agent="pytest", state={"request_id": "req-1"}
    )
    yield events_buffer
    reset_audit_context(token)


def _sso_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "specivo.sso"]


# Each case: (call, expected event, expected user_id (actor), expected detail subset).
_CASES: list[tuple[str, Callable[[], None], AuditEvent, int | None, dict[str, Any]]] = [
    (
        "user_provisioned",
        lambda: events.user_provisioned(provider_slug="idp", user_id=7, subject=SUB, issuer=ISS),
        AuditEvent.SSO_USER_PROVISIONED,
        7,
        {"provider": "idp", "iss": ISS, "sub_hash": subject_hash(SUB), "user_id": 7},
    ),
    (
        "account_linked",
        lambda: events.account_linked(provider_slug="idp", user_id=7, subject=SUB, method="user_link", issuer=ISS),
        AuditEvent.SSO_ACCOUNT_LINKED,
        7,
        {"provider": "idp", "iss": ISS, "sub_hash": subject_hash(SUB), "user_id": 7, "method": "user_link"},
    ),
    (
        "account_unlinked",
        lambda: events.account_unlinked(
            provider_slug="idp", user_id=7, subject=SUB, by="admin", actor_id=1, sessions_revoked=2, issuer=ISS
        ),
        AuditEvent.SSO_ACCOUNT_UNLINKED,
        1,
        {"provider": "idp", "sub_hash": subject_hash(SUB), "user_id": 7, "by": "admin", "sessions_revoked": 2},
    ),
    (
        "roles_synced",
        lambda: events.roles_synced(
            provider_slug="idp",
            user_id=7,
            subject=SUB,
            admin_before=False,
            admin_after=True,
            groups_added=[5, 3],
            groups_removed=[9],
        ),
        AuditEvent.SSO_ROLES_SYNCED,
        7,
        {
            "provider": "idp",
            "sub_hash": subject_hash(SUB),
            "admin_before": False,
            "admin_after": True,
            "groups_added": [3, 5],
            "groups_removed": [9],
        },
    ),
    (
        "groups_overage",
        lambda: events.groups_overage(provider_slug="idp", user_id=None, subject=SUB, denied=True),
        AuditEvent.SSO_GROUPS_OVERAGE,
        None,
        {"provider": "idp", "sub_hash": subject_hash(SUB), "denied": True},
    ),
    (
        "logout",
        lambda: events.logout(provider_slug="idp", issuer=ISS, user_id=7, subject=SUB, idp_redirect=True),
        AuditEvent.SSO_LOGOUT,
        7,
        {"provider": "idp", "iss": ISS, "sub_hash": subject_hash(SUB), "idp_redirect": True},
    ),
    (
        "backchannel_logout",
        lambda: events.backchannel_logout(provider_slug="idp", issuer=ISS, subject=SUB, by="sid", revoked=3),
        AuditEvent.SSO_BACKCHANNEL_LOGOUT,
        None,
        {"provider": "idp", "iss": ISS, "sub_hash": subject_hash(SUB), "by": "sid", "sessions_revoked": 3},
    ),
    (
        "provider_changed",
        lambda: events.provider_changed(
            AuditEvent.SSO_PROVIDER_UPDATED,
            provider_id=4,
            provider_slug="idp",
            issuer=ISS,
            actor_id=1,
            fields=["display_name"],
        ),
        AuditEvent.SSO_PROVIDER_UPDATED,
        1,
        {"provider": "idp", "iss": ISS, "provider_id": 4, "fields": ["display_name"]},
    ),
    (
        "user_deprovisioned",
        lambda: events.user_deprovisioned(
            user_id=7, actor_id=None, reason="idp_account_removed", provider_slug="idp", sessions_revoked=2
        ),
        AuditEvent.USER_DEPROVISIONED,
        None,
        {"provider": "idp", "user_id": 7, "reason": "idp_account_removed", "sessions_revoked": 2},
    ),
    (
        "user_deprovisioned_backchannel",
        lambda: events.user_deprovisioned(
            user_id=7,
            actor_id=None,
            reason="sso_backchannel",
            provider_slug="idp",
            issuer=ISS,
            subject=SUB,
            sessions_revoked=2,
            api_keys_deactivated=3,
        ),
        AuditEvent.USER_DEPROVISIONED,
        None,
        {
            "provider": "idp",
            "iss": ISS,
            "sub_hash": subject_hash(SUB),
            "user_id": 7,
            "reason": "sso_backchannel",
            "sessions_revoked": 2,
            "api_keys_deactivated": 3,
        },
    ),
    (
        "session_rejected",
        lambda: events.session_rejected(
            provider_slug="idp",
            issuer=ISS,
            user_id=7,
            subject=SUB,
            reason="idp_refresh_rejected",
            idp_error="invalid_grant",
        ),
        AuditEvent.SSO_SESSION_REJECTED,
        None,
        {
            "provider": "idp",
            "iss": ISS,
            "sub_hash": subject_hash(SUB),
            "user_id": 7,
            "reason": "idp_refresh_rejected",
            "idp_error": "invalid_grant",
        },
    ),
]


@pytest.mark.parametrize(("name", "call", "event", "actor", "details"), _CASES, ids=[c[0] for c in _CASES])
def test_event_is_recorded_and_logged_when_audit_is_on(
    audit_on: None,
    buffer: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
    name: str,
    call: Callable[[], None],
    event: AuditEvent,
    actor: int | None,
    details: dict[str, Any],
) -> None:
    caplog.set_level(logging.DEBUG, logger="specivo.sso")
    call()

    assert len(buffer) == 1, buffer
    [row] = buffer
    assert row["event_type"] == event.value
    assert row["user_id"] == actor
    assert row["ip_address"] == "203.0.113.7"
    assert row["request_id"] == "req-1"
    for key, value in details.items():
        assert row["details"][key] == value, key
    assert SUB not in repr(row)

    [record] = [r for r in _sso_records(caplog) if getattr(r, "event", None) == event.value]
    assert record.request_id == "req-1"  # type: ignore[attr-defined]
    assert SUB not in record.getMessage()
    assert SUB not in repr(record.__dict__)


@pytest.mark.parametrize(("name", "call", "event", "actor", "details"), _CASES, ids=[c[0] for c in _CASES])
def test_only_the_log_line_without_the_audit_feature(
    audit_off: None,
    buffer: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
    name: str,
    call: Callable[[], None],
    event: AuditEvent,
    actor: int | None,
    details: dict[str, Any],
) -> None:
    caplog.set_level(logging.DEBUG, logger="specivo.sso")
    call()

    assert buffer == []
    assert [r for r in _sso_records(caplog) if getattr(r, "event", None) == event.value]


def test_without_a_bound_context_the_event_is_only_logged(audit_on: None, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="specivo.sso")
    events.user_provisioned(provider_slug="idp", user_id=7, subject=SUB)
    assert [r for r in _sso_records(caplog) if getattr(r, "event", None) == "sso_user_provisioned"]
    assert current_request_id() is None


def test_a_failing_audit_write_never_breaks_the_caller(
    audit_on: None,
    buffer: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("audit store down")

    monkeypatch.setattr(events, "record_event", boom)
    caplog.set_level(logging.DEBUG, logger="specivo.sso")

    events.account_linked(provider_slug="idp", user_id=7, subject=SUB, method="auto_email")

    messages = [r.getMessage() for r in _sso_records(caplog)]
    assert any(m.startswith("sso.account.linked ") for m in messages)
    assert any(m.startswith("sso.audit.failed ") for m in messages)


def test_record_event_rejects_unknown_types(audit_on: None, buffer: list[dict[str, Any]]) -> None:
    with pytest.raises(ValueError):
        record_event("not_an_event")


def test_request_id_is_truncated_to_the_column(audit_on: None) -> None:
    rows: list[dict[str, Any]] = []
    token = bind_audit_context(rows, state={"request_id": "x" * 80})
    try:
        assert record_event(AuditEvent.SESSIONS_REVOKED, details={"reason": "logout"})
    finally:
        reset_audit_context(token)
    assert rows[0]["request_id"] == "x" * 36


def test_new_event_types_fit_the_column() -> None:
    names = [
        "SSO_LOGIN_SUCCESS",
        "SSO_LOGIN_FAILURE",
        "SSO_USER_PROVISIONED",
        "SSO_ACCOUNT_LINKED",
        "SSO_ACCOUNT_UNLINKED",
        "SSO_ROLES_SYNCED",
        "SSO_GROUPS_OVERAGE",
        "SSO_LOGOUT",
        "SSO_BACKCHANNEL_LOGOUT",
        "SSO_PROVIDER_CREATED",
        "SSO_PROVIDER_UPDATED",
        "SSO_PROVIDER_DELETED",
        "SSO_PROVIDER_SECRET_CHANGED",
        "USER_DEPROVISIONED",
        "SESSIONS_REVOKED",
        "SSO_SESSION_REJECTED",
    ]
    for name in names:
        member = AuditEvent[name]
        assert member.value == name.lower()
        assert len(member.value) <= 50


# ---------------------------------------------------------------------------
# Redaction of URLs and headers (httpx and the SSO logger)
# ---------------------------------------------------------------------------


def test_redact_query_parameters_and_authorization() -> None:
    line = 'HTTP Request: GET http://test/auth/oidc/idp/callback/?code=abc123&state=xyz789&iss=https%3A%2F%2Fidp "200"'
    cleaned = redact(line)
    assert "abc123" not in cleaned and "xyz789" not in cleaned
    assert "code=[redacted]" in cleaned and "state=[redacted]" in cleaned
    assert "iss=https%3A%2F%2Fidp" in cleaned
    assert "secret-bearer" not in redact("Authorization: Bearer secret-bearer")
    assert "c2VjcmV0" not in redact("headers={'authorization': 'Basic c2VjcmV0'}")
    # A space-separated log field named code is a reason code, not an OAuth code.
    assert redact("sso.login.failed code=sso_idp_error") == "sso.login.failed code=sso_idp_error"
    assert REDACTED in redact("x eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln")


def test_httpx_request_log_line_is_redacted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="httpx")
    logging.getLogger("httpx").info(
        'HTTP Request: %s %s "%s %d %s"',
        "GET",
        "http://test/auth/oidc/idp/callback/?state=s3cr3t-state&code=s3cr3t-code",
        "HTTP/1.1",
        302,
        "Found",
    )
    [record] = [r for r in caplog.records if r.name == "httpx"]
    assert "s3cr3t-code" not in record.getMessage()
    assert "s3cr3t-state" not in record.getMessage()
    assert "s3cr3t" not in caplog.text
