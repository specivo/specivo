"""WebSocket connection manager for real-time notification delivery."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from specivo.core.ws_envelope import WSEnvelope

logger = logging.getLogger(__name__)


class WSConnectionManager:
    """Track active WebSocket connections per user."""

    def __init__(self) -> None:
        self._connections: dict[int, list[Any]] = {}

    async def connect(self, user_id: int, ws: Any) -> None:
        """Register a WebSocket connection for a user."""
        if user_id not in self._connections:
            self._connections[user_id] = []
        self._connections[user_id].append(ws)

    def disconnect(self, user_id: int, ws: Any) -> None:
        """Remove a WebSocket connection for a user."""
        conns = self._connections.get(user_id)
        if conns is None:
            return
        try:
            conns.remove(ws)
        except ValueError:
            pass
        if not conns:
            del self._connections[user_id]

    async def send_to_user(self, user_id: int, envelope: WSEnvelope) -> None:
        """Send an envelope to all active connections for a user."""
        conns = self._connections.get(user_id)
        if not conns:
            return
        data = envelope.to_dict()
        for ws in list(conns):
            try:
                await ws.send_json(data)
            except Exception:
                logger.warning("Failed to send WS message to user %s", user_id)

    def has_connections(self, user_id: int) -> bool:
        """Check if a user has any active connections."""
        conns = self._connections.get(user_id)
        return bool(conns)
