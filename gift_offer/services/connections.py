from __future__ import annotations

import logging

from ..models import BusinessConnection
from ..telegram import TransportError
from .context import ConnectionState, Runtime

log = logging.getLogger("gift_offer.service")


class ConnectionService:
    """Refresh and cache business connections without changing Telegram payloads."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.state = ConnectionState()

    async def connection(self, connection_id: str) -> BusinessConnection:
        now = int(self.runtime.clock())
        if (
            connection_id not in self.state.connections
            or now - self.state.checked_at.get(connection_id, 0) >= 60
        ):
            connection = await self.runtime.api.call(
                "getBusinessConnection", business_connection_id=connection_id
            )
            user = connection.get("user") if isinstance(connection, dict) else None
            if (
                not isinstance(user, dict)
                or connection.get("id") != connection_id
                or type(user.get("id")) is not int
                or user["id"] <= 0
                or type(connection.get("user_chat_id")) is not int
                or connection["user_chat_id"] <= 0
            ):
                raise TransportError("Telegram returned an incomplete business connection")
            self.state.connections[connection_id] = connection
            self.state.checked_at[connection_id] = now
            self.runtime.store.save_connection(connection)
            self.runtime.store.touch_user(connection["user"], now)
        return self.state.connections[connection_id]
