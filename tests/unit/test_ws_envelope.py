"""Unit tests for WSEnvelope — the versioned WebSocket message format.

RED phase — these tests define the expected behaviour of WSEnvelope before
any implementation exists.

Covers:
- WSEnvelope construction with all fields
- to_dict() produces a JSON-serialisable dict with correct keys
- Version field 'v' is present and set to the expected integer
- Envelope is frozen (immutable)
- Notification, ping, and pong type variants
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestWSEnvelopeConstruction:
    def test_all_fields_accepted(self):
        """WSEnvelope can be constructed with v, type, payload, and ts."""
        from specivo.core.ws_envelope import WSEnvelope

        env = WSEnvelope(
            v=1,
            type="notification",
            payload={"id": 42, "title": "Test"},
            ts="2026-04-03T10:30:00Z",
        )
        assert env.v == 1
        assert env.type == "notification"
        assert env.payload == {"id": 42, "title": "Test"}
        assert env.ts == "2026-04-03T10:30:00Z"

    def test_envelope_is_frozen(self):
        """WSEnvelope must be immutable (frozen dataclass)."""
        from specivo.core.ws_envelope import WSEnvelope

        env = WSEnvelope(v=1, type="ping", payload={}, ts="2026-04-03T10:00:00Z")
        with pytest.raises((AttributeError, TypeError)):
            env.v = 2  # type: ignore[misc]

    def test_empty_payload_is_valid(self):
        """An empty dict is a valid payload (e.g. for ping/pong)."""
        from specivo.core.ws_envelope import WSEnvelope

        env = WSEnvelope(v=1, type="ping", payload={}, ts="2026-04-03T10:00:00Z")
        assert env.payload == {}


# ---------------------------------------------------------------------------
# to_dict
# ---------------------------------------------------------------------------


class TestWSEnvelopeToDict:
    def test_to_dict_returns_dict(self):
        """to_dict() must return a plain dict."""
        from specivo.core.ws_envelope import WSEnvelope

        env = WSEnvelope(v=1, type="notification", payload={"id": 1}, ts="2026-04-03T10:00:00Z")
        result = env.to_dict()
        assert isinstance(result, dict)

    def test_to_dict_has_v_key(self):
        """to_dict() output must include the 'v' key."""
        from specivo.core.ws_envelope import WSEnvelope

        env = WSEnvelope(v=1, type="notification", payload={}, ts="2026-04-03T10:00:00Z")
        assert "v" in env.to_dict()

    def test_to_dict_has_type_key(self):
        """to_dict() output must include the 'type' key."""
        from specivo.core.ws_envelope import WSEnvelope

        env = WSEnvelope(v=1, type="ping", payload={}, ts="2026-04-03T10:00:00Z")
        assert "type" in env.to_dict()

    def test_to_dict_has_payload_key(self):
        """to_dict() output must include the 'payload' key."""
        from specivo.core.ws_envelope import WSEnvelope

        env = WSEnvelope(v=1, type="notification", payload={"foo": "bar"}, ts="2026-04-03T10:00:00Z")
        assert "payload" in env.to_dict()

    def test_to_dict_has_ts_key(self):
        """to_dict() output must include the 'ts' key."""
        from specivo.core.ws_envelope import WSEnvelope

        env = WSEnvelope(v=1, type="notification", payload={}, ts="2026-04-03T10:00:00Z")
        assert "ts" in env.to_dict()

    def test_to_dict_values_match_fields(self):
        """to_dict() values match the fields passed at construction."""
        from specivo.core.ws_envelope import WSEnvelope

        env = WSEnvelope(
            v=1,
            type="notification",
            payload={"id": 42, "title": "Assigned to you"},
            ts="2026-04-03T10:30:00Z",
        )
        d = env.to_dict()
        assert d["v"] == 1
        assert d["type"] == "notification"
        assert d["payload"] == {"id": 42, "title": "Assigned to you"}
        assert d["ts"] == "2026-04-03T10:30:00Z"

    def test_to_dict_is_json_serialisable(self):
        """to_dict() output must be serialisable by json.dumps without error."""
        import json

        from specivo.core.ws_envelope import WSEnvelope

        env = WSEnvelope(
            v=1,
            type="notification",
            payload={"id": 5, "event_type": "assignment"},
            ts="2026-04-03T10:30:00Z",
        )
        # Should not raise
        json.dumps(env.to_dict())


# ---------------------------------------------------------------------------
# Version field
# ---------------------------------------------------------------------------


class TestWSEnvelopeVersion:
    def test_protocol_version_is_one(self):
        """The initial protocol version must be 1."""
        from specivo.core.ws_envelope import WSEnvelope

        env = WSEnvelope(v=1, type="notification", payload={}, ts="2026-04-03T10:00:00Z")
        assert env.v == 1

    def test_version_is_integer(self):
        """The version field must be an integer."""
        from specivo.core.ws_envelope import WSEnvelope

        env = WSEnvelope(v=1, type="notification", payload={}, ts="2026-04-03T10:00:00Z")
        assert isinstance(env.v, int)


# ---------------------------------------------------------------------------
# Message type variants
# ---------------------------------------------------------------------------


class TestWSEnvelopeTypes:
    def test_notification_type(self):
        """type='notification' is a valid envelope type."""
        from specivo.core.ws_envelope import WSEnvelope

        env = WSEnvelope(
            v=1,
            type="notification",
            payload={
                "id": 42,
                "event_type": "assignment",
                "entity_type": "issue",
                "entity_id": 15,
                "project_id": 1,
                "actor_id": 5,
                "title": "[ACME-15] Assigned to you by Alice",
                "body": "Fix the login bug",
                "unread_count": 7,
            },
            ts="2026-04-03T10:30:00Z",
        )
        assert env.type == "notification"

    def test_ping_type(self):
        """type='ping' with empty payload is a valid keepalive envelope."""
        from specivo.core.ws_envelope import WSEnvelope

        env = WSEnvelope(v=1, type="ping", payload={}, ts="2026-04-03T10:30:00Z")
        assert env.type == "ping"
        assert env.payload == {}

    def test_pong_type(self):
        """type='pong' with empty payload is a valid keepalive response envelope."""
        from specivo.core.ws_envelope import WSEnvelope

        env = WSEnvelope(v=1, type="pong", payload={}, ts="2026-04-03T10:30:00Z")
        assert env.type == "pong"
        assert env.payload == {}

    def test_error_type(self):
        """type='error' is a valid envelope type for protocol error messages."""
        from specivo.core.ws_envelope import WSEnvelope

        env = WSEnvelope(
            v=1,
            type="error",
            payload={"code": "auth_required"},
            ts="2026-04-03T10:30:00Z",
        )
        assert env.type == "error"
