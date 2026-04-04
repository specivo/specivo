"""Unit tests for WSConnectionManager.

RED phase — these tests define the expected behaviour of the WebSocket
connection manager before any implementation exists.

Covers:
- connect() adds a WebSocket to the user's connection list
- disconnect() removes a WebSocket from the user's connection list
- send_to_user() calls ws.send_json for each connection
- send_to_user() silently does nothing when user has no connections
- has_connections() returns True when connections exist for a user
- has_connections() returns False when no connections exist
- disconnect() of one connection does not affect other connections for same user
- Multiple users are tracked independently
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ws() -> MagicMock:
    """Build a mock WebSocket with an async send_json method."""
    ws = MagicMock()
    ws.send_json = AsyncMock()
    return ws


def _make_envelope() -> MagicMock:
    """Build a minimal mock WSEnvelope with a to_dict() method."""
    env = MagicMock()
    env.to_dict.return_value = {"v": 1, "type": "ping", "payload": {}, "ts": "2026-04-03T10:00:00Z"}
    return env


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


class TestWSConnectionManagerConnect:
    async def test_connect_adds_connection_for_user(self):
        """After connect(), has_connections() returns True for that user."""
        from specivo.services.ws_manager import WSConnectionManager

        manager = WSConnectionManager()
        ws = _make_ws()
        await manager.connect(user_id=1, ws=ws)
        assert manager.has_connections(user_id=1) is True

    async def test_connect_multiple_connections_for_same_user(self):
        """A user can have multiple simultaneous WebSocket connections."""
        from specivo.services.ws_manager import WSConnectionManager

        manager = WSConnectionManager()
        ws1 = _make_ws()
        ws2 = _make_ws()
        await manager.connect(user_id=1, ws=ws1)
        await manager.connect(user_id=1, ws=ws2)
        # Both should receive messages later; we confirm two are tracked
        assert manager.has_connections(user_id=1) is True

    async def test_connect_different_users_are_tracked_independently(self):
        """Connecting user 1 must not create connections for user 2."""
        from specivo.services.ws_manager import WSConnectionManager

        manager = WSConnectionManager()
        await manager.connect(user_id=1, ws=_make_ws())
        assert manager.has_connections(user_id=2) is False


# ---------------------------------------------------------------------------
# disconnect
# ---------------------------------------------------------------------------


class TestWSConnectionManagerDisconnect:
    async def test_disconnect_removes_connection(self):
        """After disconnect(), has_connections() returns False when last WS removed."""
        from specivo.services.ws_manager import WSConnectionManager

        manager = WSConnectionManager()
        ws = _make_ws()
        await manager.connect(user_id=1, ws=ws)
        manager.disconnect(user_id=1, ws=ws)
        assert manager.has_connections(user_id=1) is False

    async def test_disconnect_one_connection_leaves_others(self):
        """Disconnecting one WS must not remove other connections for the same user."""
        from specivo.services.ws_manager import WSConnectionManager

        manager = WSConnectionManager()
        ws1 = _make_ws()
        ws2 = _make_ws()
        await manager.connect(user_id=1, ws=ws1)
        await manager.connect(user_id=1, ws=ws2)
        manager.disconnect(user_id=1, ws=ws1)
        # ws2 still connected
        assert manager.has_connections(user_id=1) is True

    async def test_disconnect_does_not_affect_other_users(self):
        """Disconnecting user 1 must not remove user 2's connections."""
        from specivo.services.ws_manager import WSConnectionManager

        manager = WSConnectionManager()
        ws1 = _make_ws()
        ws2 = _make_ws()
        await manager.connect(user_id=1, ws=ws1)
        await manager.connect(user_id=2, ws=ws2)
        manager.disconnect(user_id=1, ws=ws1)
        assert manager.has_connections(user_id=2) is True

    def test_disconnect_unknown_user_does_not_raise(self):
        """Calling disconnect() for a user with no connections must not raise."""
        from specivo.services.ws_manager import WSConnectionManager

        manager = WSConnectionManager()
        # Should not raise even though user 99 was never connected
        manager.disconnect(user_id=99, ws=_make_ws())


# ---------------------------------------------------------------------------
# send_to_user
# ---------------------------------------------------------------------------


class TestWSConnectionManagerSendToUser:
    async def test_send_to_user_calls_send_json(self):
        """send_to_user() must call ws.send_json() for the connected WebSocket."""
        from specivo.services.ws_manager import WSConnectionManager

        manager = WSConnectionManager()
        ws = _make_ws()
        await manager.connect(user_id=1, ws=ws)
        env = _make_envelope()
        await manager.send_to_user(user_id=1, envelope=env)
        ws.send_json.assert_called_once_with(env.to_dict())

    async def test_send_to_user_sends_to_all_connections(self):
        """send_to_user() must send to ALL connections for the user."""
        from specivo.services.ws_manager import WSConnectionManager

        manager = WSConnectionManager()
        ws1 = _make_ws()
        ws2 = _make_ws()
        await manager.connect(user_id=1, ws=ws1)
        await manager.connect(user_id=1, ws=ws2)
        env = _make_envelope()
        await manager.send_to_user(user_id=1, envelope=env)
        ws1.send_json.assert_called_once()
        ws2.send_json.assert_called_once()

    async def test_send_to_user_silently_skips_when_no_connections(self):
        """send_to_user() must not raise when the user has no connections."""
        from specivo.services.ws_manager import WSConnectionManager

        manager = WSConnectionManager()
        env = _make_envelope()
        # Should not raise
        await manager.send_to_user(user_id=99, envelope=env)

    async def test_send_to_user_does_not_send_to_other_users(self):
        """send_to_user(user_id=1) must not send to user 2's connections."""
        from specivo.services.ws_manager import WSConnectionManager

        manager = WSConnectionManager()
        ws1 = _make_ws()
        ws2 = _make_ws()
        await manager.connect(user_id=1, ws=ws1)
        await manager.connect(user_id=2, ws=ws2)
        env = _make_envelope()
        await manager.send_to_user(user_id=1, envelope=env)
        ws2.send_json.assert_not_called()


# ---------------------------------------------------------------------------
# has_connections
# ---------------------------------------------------------------------------


class TestWSConnectionManagerHasConnections:
    def test_returns_false_for_new_manager(self):
        """A freshly created manager has no connections for any user."""
        from specivo.services.ws_manager import WSConnectionManager

        manager = WSConnectionManager()
        assert manager.has_connections(user_id=1) is False

    async def test_returns_true_after_connect(self):
        """has_connections() returns True after connect() for that user."""
        from specivo.services.ws_manager import WSConnectionManager

        manager = WSConnectionManager()
        await manager.connect(user_id=42, ws=_make_ws())
        assert manager.has_connections(user_id=42) is True

    async def test_returns_false_after_all_disconnected(self):
        """has_connections() returns False once all connections are removed."""
        from specivo.services.ws_manager import WSConnectionManager

        manager = WSConnectionManager()
        ws = _make_ws()
        await manager.connect(user_id=1, ws=ws)
        manager.disconnect(user_id=1, ws=ws)
        assert manager.has_connections(user_id=1) is False
