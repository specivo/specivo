"""The pure parts of the Hermes event receiver: the mapping table, signatures, timestamps, the Hermes block."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from specivo.services.hermes_event_service import (
    KIND_APPROVAL,
    KIND_SESSION_END,
    KIND_SESSION_START,
    KIND_SUBAGENT_START,
    KIND_SUBAGENT_STOP,
    KIND_TOOL_CALL,
    check_timestamp,
    delivery_id_of,
    map_event,
    verify_signature,
)
from specivo.services.hermes_receiver_service import SUBSCRIBED_EVENTS, TOKEN_PATTERN, hooks_config
from specivo.services.security_audit_service import EVENT_CATEGORIES, AuditCategory, AuditEvent
from tests.fixtures.hermes_events import CANARY, payload, sign

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("event", "extra", "kind", "audit", "details"),
    [
        (
            "on_session_start",
            {"model": "lab-model-1", "platform": "cli"},
            KIND_SESSION_START,
            AuditEvent.AGENT_SESSION_STARTED,
            {"model": "lab-model-1", "platform": "cli", "profile": "default"},
        ),
        (
            "post_tool_call",
            {
                "status": "error",
                "error_type": "tool_error",
                "error_message": CANARY,
                "duration_ms": 7,
                "result": CANARY,
            },
            KIND_TOOL_CALL,
            AuditEvent.AGENT_TOOL_CALLED,
            {"tool": "terminal", "status": "error", "error_type": "tool_error", "duration_ms": 7},
        ),
        (
            "pre_approval_request",
            {"command": CANARY, "description": CANARY, "pattern_key": "k", "pattern_keys": ["k", 3], "surface": "cli"},
            KIND_APPROVAL,
            AuditEvent.AGENT_APPROVAL_REQUESTED,
            {"pattern_key": "k", "pattern_keys": ["k"], "surface": "cli"},
        ),
        (
            "post_approval_response",
            {"command": CANARY, "choice": "deny", "decided_by": "aux_llm", "pattern_key": "k"},
            KIND_APPROVAL,
            AuditEvent.AGENT_APPROVAL_RESOLVED,
            {"choice": "deny", "decided_by": "aux_llm", "pattern_key": "k"},
        ),
        (
            "subagent_start",
            {"child_session_id": "c1", "child_role": None, "child_subagent_id": "sa", "child_goal": CANARY},
            KIND_SUBAGENT_START,
            AuditEvent.AGENT_SUBAGENT_STARTED,
            {"child_session_id": "c1", "child_subagent_id": "sa"},
        ),
        (
            "subagent_stop",
            {
                "child_session_id": "c1",
                "child_status": "failed",
                "child_summary": CANARY,
                "duration_ms": 1234,
                "tool_call_history": [{"tool_name": "write_file", "tool_input": {"x": CANARY}}, "junk"],
            },
            KIND_SUBAGENT_STOP,
            AuditEvent.AGENT_SUBAGENT_STOPPED,
            {"child_session_id": "c1", "child_status": "failed", "duration_ms": 1234, "tools": ["write_file"]},
        ),
        (
            "on_session_end",
            {"completed": False, "interrupted": True, "turn_exit_reason": CANARY},
            KIND_SESSION_END,
            AuditEvent.AGENT_SESSION_ENDED,
            {"completed": False, "interrupted": True},
        ),
    ],
)
def test_the_mapping_table(event: str, extra: dict, kind: str, audit: AuditEvent, details: dict) -> None:
    mapped = map_event(payload(event, "s1", tool_name="terminal", tool_input={"cmd": CANARY}, extra=extra))
    assert mapped is not None
    assert mapped.kind == kind
    assert mapped.audit_event == audit
    assert mapped.session_id == "s1"
    assert mapped.details == details
    assert CANARY not in str(mapped)


@pytest.mark.parametrize(
    "event", ["post_api_request", "pre_llm_call", "post_llm_call", "pre_tool_call", "on_session_finalize", "nope"]
)
def test_other_events_are_not_mapped(event: str) -> None:
    assert map_event(payload(event, "s1")) is None


def test_an_event_without_a_session_id_is_not_mapped() -> None:
    assert map_event(payload("on_session_start", "")) is None
    assert map_event({"hook_event_name": "on_session_start"}) is None


def test_long_values_are_cut() -> None:
    mapped = map_event(payload("post_tool_call", "s1", tool_name="t" * 500))
    assert mapped is not None
    assert len(mapped.details["tool"]) == 200


def test_every_new_audit_event_has_a_category() -> None:
    hermes = [
        e for e in AuditEvent if e.name.startswith(("AGENT_", "HERMES_")) and e != AuditEvent.AGENT_SESSION_LINKED
    ]
    assert len(hermes) == 11
    assert all(e in EVENT_CATEGORIES for e in hermes)
    assert EVENT_CATEGORIES[AuditEvent.HERMES_RECEIVER_CREATED] == AuditCategory.ADMINISTRATION
    assert EVENT_CATEGORIES[AuditEvent.AGENT_TOOL_CALLED] == AuditCategory.AGENTS


def test_signatures() -> None:
    body = b'{"a": 1}'
    assert verify_signature("s3cret", body, sign("s3cret", body))
    assert verify_signature("s3cret", body, sign("s3cret", body).upper().replace("SHA256=", "sha256="))
    assert not verify_signature("s3cret", body, sign("other", body))
    assert not verify_signature("s3cret", body + b" ", sign("s3cret", body))
    assert not verify_signature("s3cret", body, sign("s3cret", body).removeprefix("sha256="))
    assert not verify_signature("s3cret", body, None)
    assert not verify_signature("s3cret", body, "")


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("2026-11-23T12:00:00Z", True),
        ("2026-11-23T12:09:59.999999Z", True),
        ("2026-11-23T11:50:00+00:00", True),
        ("2026-11-23T19:00:00+07:00", True),
        ("2026-11-23T11:49:59Z", False),
        ("2026-11-23T12:10:01Z", False),
        ("2026-11-23T12:00:00", False),
        ("", False),
        (None, False),
        (1795447178, False),
        ("yesterday", False),
    ],
)
def test_timestamps(value: object, ok: bool) -> None:
    assert check_timestamp(value, now=NOW) is ok


def test_timestamp_window_is_ten_minutes() -> None:
    assert check_timestamp((NOW - timedelta(minutes=10)).isoformat(), now=NOW)


@pytest.mark.parametrize(
    ("value", "ok"),
    [("0f" * 16, True), ("abc", True), ("", False), (None, False), ("x" * 65, False), ("a b", False), (7, False)],
)
def test_delivery_ids(value: object, ok: bool) -> None:
    assert (delivery_id_of({"delivery_id": value}) is not None) is ok


def test_the_hermes_block_names_the_url_events_and_secret_env() -> None:
    token = "T" * 43
    assert TOKEN_PATTERN.fullmatch(token)
    config = hooks_config(token)
    assert config.startswith("hooks:\n  outbound:\n    - name: specivo\n")
    assert f"/api/v1/hermes/events/{token}/\n" in config
    assert "events: [" + ", ".join(SUBSCRIBED_EVENTS) + "]" in config
    assert "secret_env: SPECIVO_WEBHOOK_SECRET" in config
