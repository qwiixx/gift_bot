from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from .config import Config
from .models import (
    BusinessConnection,
    CallbackQuery,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    Offer,
    Update,
)
from .services.actions import OfferActionService
from .services.administration import AdministrationService
from .services.cards import CardService
from .services.common import CALLBACK as CALLBACK
from .services.common import can_reply as can_reply
from .services.common import worker_label as worker_label
from .services.connections import ConnectionService
from .services.context import Runtime
from .services.gifts import GiftService
from .services.notifications import NotificationService
from .services.offers import OfferService
from .store import Store
from .types import TelegramClient


class Service:
    """Compose application services; serialize updates and periodic maintenance.

    Legacy entry points and mutable state access remain available for callers.
    Domain services receive explicit dependencies, never this facade.
    """

    def __init__(
        self,
        config: Config,
        store: Store,
        api: TelegramClient,
        bot_id: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.runtime = Runtime(config, store, api, bot_id, clock)
        self.lock = asyncio.Lock()
        self.connections_service = ConnectionService(self.runtime)
        self.notifications_service = NotificationService(self.runtime)
        self.gifts_service = GiftService(self.runtime, notifications=self.notifications_service)
        self.administration_service = AdministrationService(
            self.runtime,
            connections=self.connections_service,
            notifications=self.notifications_service,
        )
        self.cards_service = CardService(
            self.runtime,
            connections=self.connections_service,
            notifications=self.notifications_service,
        )
        self.offers_service = OfferService(
            self.runtime,
            connections=self.connections_service,
            gifts=self.gifts_service,
            notifications=self.notifications_service,
        )
        self.actions_service = OfferActionService(
            self.runtime,
            administration=self.administration_service,
            cards=self.cards_service,
            connections=self.connections_service,
            notifications=self.notifications_service,
        )

    @property
    def config(self) -> Config:
        return self.runtime.config

    @config.setter
    def config(self, value: Config) -> None:
        self.runtime.config = value

    @property
    def store(self) -> Store:
        return self.runtime.store

    @store.setter
    def store(self, value: Store) -> None:
        self.runtime.store = value

    @property
    def api(self) -> TelegramClient:
        return self.runtime.api

    @api.setter
    def api(self, value: TelegramClient) -> None:
        self.runtime.api = value

    @property
    def bot_id(self) -> int:
        return self.runtime.bot_id

    @bot_id.setter
    def bot_id(self, value: int) -> None:
        self.runtime.bot_id = value

    @property
    def clock(self) -> Callable[[], float]:
        return self.runtime.clock

    @clock.setter
    def clock(self, value: Callable[[], float]) -> None:
        self.runtime.clock = value

    @property
    def connections(self) -> dict[str, BusinessConnection]:
        return self.connections_service.state.connections

    @connections.setter
    def connections(self, value: dict[str, BusinessConnection]) -> None:
        self.connections_service.state.connections = value

    @property
    def connection_checked_at(self) -> dict[str, int]:
        return self.connections_service.state.checked_at

    @connection_checked_at.setter
    def connection_checked_at(self, value: dict[str, int]) -> None:
        self.connections_service.state.checked_at = value

    @property
    def delete_notices(self) -> set[str]:
        return self.connections_service.state.delete_notices

    @delete_notices.setter
    def delete_notices(self, value: set[str]) -> None:
        self.connections_service.state.delete_notices = value

    @property
    def retry_at(self) -> dict[str, int]:
        return self.cards_service.state.retry_at

    @retry_at.setter
    def retry_at(self, value: dict[str, int]) -> None:
        self.cards_service.state.retry_at = value

    @property
    def admin_recipient_waiting(self) -> set[int]:
        return self.administration_service.state.recipient_waiting

    @admin_recipient_waiting.setter
    def admin_recipient_waiting(self, value: set[int]) -> None:
        self.administration_service.state.recipient_waiting = value

    @property
    def transfer_confirmation_waiting(self) -> dict[tuple[str, int], int]:
        return self.actions_service.state.confirmation_waiting

    @transfer_confirmation_waiting.setter
    def transfer_confirmation_waiting(self, value: dict[tuple[str, int], int]) -> None:
        self.actions_service.state.confirmation_waiting = value

    async def handle(self, update: Update) -> None:
        async with self.lock:
            now = int(self.clock())
            self.store.expire(now)
            if "business_connection" in update:
                connection = update["business_connection"]
                self.connections[connection["id"]] = connection
                self.connection_checked_at[connection["id"]] = now
                self.delete_notices.discard(connection["id"])
                self.store.save_connection(connection)
                self.store.touch_user(connection["user"], now)
            elif "callback_query" in update:
                await self.callback(update["callback_query"], now)
            elif "business_message" in update:
                await self.business_message(update["business_message"], now)
            elif "deleted_business_messages" in update:
                deleted = update["deleted_business_messages"]
                for message_id in deleted.get("message_ids", []):
                    self.store.hide_card(
                        deleted["business_connection_id"], deleted["chat"]["id"], message_id, now
                    )
            elif "message" in update:
                await self.private_message(update["message"], now)
            # Edited commands intentionally do not alter an existing contract.
            await self._flush(now)
            await self._cleanup_commands(now)

    async def tick(self) -> None:
        async with self.lock:
            now = int(self.clock())
            self.store.expire(now)
            self.store.refresh_pending_countdowns(now)
            await self._flush(now)
            await self._cleanup_commands(now)

    async def connection(self, connection_id: str) -> BusinessConnection:
        return await self.connections_service.connection(connection_id)

    async def answer(self, query_id: str, text: str, alert: bool = False) -> None:
        return await self.notifications_service.answer(query_id, text, alert)

    async def direct(
        self, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None
    ) -> None:
        return await self.notifications_service.direct(chat_id, text, reply_markup)

    async def notify_owner(self, connection: BusinessConnection, text: str) -> None:
        return await self.notifications_service.notify_owner(connection, text)

    async def notify_topic(
        self, thread_id: int, text: str, *, link_preview_options: LinkPreviewOptions | None = None
    ) -> None:
        return await self.notifications_service.notify_topic(
            thread_id, text, link_preview_options=link_preview_options
        )

    def peer_flood_details(self, language: str, connection_id: str, chat_id: int, now: int) -> str:
        return self.notifications_service.peer_flood_details(language, connection_id, chat_id, now)

    async def telegram_gift(
        self, message: Message, connection: BusinessConnection, now: int
    ) -> None:
        return await self.gifts_service.telegram_gift(message, connection, now)

    async def private_message(self, message: Message, now: int) -> None:
        return await self.administration_service.private_message(message, now)

    async def activate_admin(self, user_id: int, chat_id: int, message_id: int, now: int) -> None:
        return await self.administration_service.activate_admin(user_id, chat_id, message_id, now)

    async def show_admin(
        self, chat_id: int, category: str = "home", before: int = 0, message_id: int | None = None
    ) -> None:
        return await self.administration_service.show_admin(chat_id, category, before, message_id)

    async def admin_callback(self, query: CallbackQuery) -> None:
        return await self.administration_service.admin_callback(query)

    async def _cleanup_commands(self, now: int) -> None:
        return await self.cards_service._cleanup_commands(now)

    async def refresh_paused_card(
        self, offer: Offer, connection: BusinessConnection, query_id: str, now: int
    ) -> bool:
        return await self.cards_service.refresh_paused_card(offer, connection, query_id, now)

    async def _flush(self, now: int) -> None:
        return await self.cards_service._flush(now)

    async def business_message(self, message: Message, now: int) -> None:
        return await self.offers_service.business_message(message, now)

    async def owner_command(
        self, connection: BusinessConnection, message: Message, command: str, now: int
    ) -> None:
        return await self.offers_service.owner_command(connection, message, command, now)

    async def callback(self, query: CallbackQuery, now: int) -> None:
        return await self.actions_service.callback(query, now)
