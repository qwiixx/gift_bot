"""Explicit runtime dependencies and state owned by each application service."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..config import Config
from ..models import BusinessConnection
from ..store import Store
from ..types import TelegramClient


@dataclass
class Runtime:
    """Shared dependencies; replacement through the facade reaches every service."""

    config: Config
    store: Store
    api: TelegramClient
    bot_id: int
    clock: Callable[[], float]


@dataclass
class ConnectionState:
    connections: dict[str, BusinessConnection] = field(default_factory=dict)
    checked_at: dict[str, int] = field(default_factory=dict)
    delete_notices: set[str] = field(default_factory=set)


@dataclass
class CardState:
    retry_at: dict[str, int] = field(default_factory=dict)


@dataclass
class AdministrationState:
    recipient_waiting: set[int] = field(default_factory=set)


@dataclass
class OfferActionState:
    confirmation_waiting: dict[tuple[str, int], int] = field(default_factory=dict)
