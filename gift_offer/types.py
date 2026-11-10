"""Small interfaces shared by the runtime and test doubles."""

from __future__ import annotations

from typing import Any, Protocol, TypedDict


class TelegramClient(Protocol):
    """Transport boundary; Telegram API results depend on the requested method."""

    async def call(self, method: str, **params: Any) -> Any: ...


class HealthState(TypedDict):
    ready: bool
    last_poll: float
