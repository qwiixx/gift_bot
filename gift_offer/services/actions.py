from __future__ import annotations

import logging

from ..domain import OfferError, next_status
from ..models import CallbackQuery
from ..telegram import TelegramError, TransportError
from ..views import language_for, tr
from .administration import AdministrationService
from .cards import CardService
from .common import CALLBACK, can_reply
from .connections import ConnectionService
from .context import OfferActionState, Runtime
from .notifications import NotificationService

log = logging.getLogger("gift_offer.service")


class OfferActionService:
    """Validate offer callbacks and apply participant statements."""

    def __init__(
        self,
        runtime: Runtime,
        administration: AdministrationService,
        cards: CardService,
        connections: ConnectionService,
        notifications: NotificationService,
    ) -> None:
        self.runtime = runtime
        self.administration = administration
        self.cards = cards
        self.connections = connections
        self.notifications = notifications
        self.state = OfferActionState()

    async def callback(self, query: CallbackQuery, now: int) -> None:
        user = query.get("from", {})
        language = language_for(user, self.runtime.config.default_language)
        query_id = query.get("id")
        if not query_id:
            return
        if query.get("data", "").startswith("a:"):
            await self.administration.admin_callback(query)
            return
        match = CALLBACK.fullmatch(query.get("data", ""))
        if not match:
            await self.notifications.answer(query_id, tr(language, "unknown_action"), True)
            return
        offer_id, action = match.groups()
        offer = self.runtime.store.offer(offer_id)
        if not offer:
            await self.notifications.answer(query_id, tr(language, "not_found"), True)
            return
        language = offer["language"]
        if user.get("id") not in {offer["buyer_id"], offer["seller_id"]} or user.get("is_bot"):
            await self.notifications.answer(query_id, tr(language, "wrong_role"), True)
            return
        message = query.get("message", {})
        if (
            message.get("business_connection_id") != offer["connection_id"]
            or message.get("chat", {}).get("id") != offer["chat_id"]
            or not offer["display_enabled"]
            or not message.get("message_id")
            or message.get("date") == 0
        ):
            await self.notifications.answer(query_id, tr(language, "wrong_card"), True)
            return
        try:
            connection = await self.connections.connection(offer["connection_id"])
        except (TelegramError, TransportError) as exc:
            if isinstance(exc, TransportError) or exc.code == 429 or exc.code >= 500:
                raise
            await self.notifications.answer(query_id, tr(language, "connection_disabled"), True)
            return
        if not can_reply(connection) or connection["user"]["id"] != offer["buyer_id"]:
            await self.notifications.answer(query_id, tr(language, "connection_disabled"), True)
            return
        if offer["card_message_id"] is None and offer["status"] == "delivery_unknown":
            # Recover only an actual message sent by this bot on this connection.
            if message.get("sender_business_bot", {}).get("id") != self.runtime.bot_id:
                await self.notifications.answer(query_id, tr(language, "wrong_card"), True)
                return
            offer = self.runtime.store.recover_card(offer_id, message["message_id"], now)
        if message["message_id"] != offer["card_message_id"]:
            await self.notifications.answer(query_id, tr(language, "wrong_card"), True)
            return
        paused = not self.runtime.store.can_update_card(
            offer["connection_id"], offer["chat_id"], now
        )
        repeated = False
        try:
            # Verify the action's role and state before attempting a manual refresh.
            next_status(offer, action, user["id"], now)
        except OfferError as exc:
            if not paused or exc.code != "already_done":
                await self.notifications.answer(query_id, tr(language, exc.code), True)
                return
            # A prior click may already be saved while its card edit was refused.
            # Refresh that state without recording the action for a second time.
            repeated = True
        if action == "confirm_transfer":
            confirmation_key = (offer_id, user["id"])
            first_click_at = self.state.confirmation_waiting.get(confirmation_key)
            if first_click_at is None or now - first_click_at > 30:
                self.state.confirmation_waiting[confirmation_key] = now
                await self.notifications.answer(
                    query_id, "Telegram проверяет вашу передачу. Пожалуйста, подождите.", True
                )
                return
            self.state.confirmation_waiting.pop(confirmation_key, None)
        if paused and not await self.cards.refresh_paused_card(offer, connection, query_id, now):
            return
        if repeated:
            await self.notifications.answer(query_id, tr(language, "already_done"), True)
            return
        self.runtime.store.touch_user(user, now)
        try:
            self.runtime.store.transition(offer_id, action, user["id"], now)
        except OfferError as exc:
            if exc.code == "expired":
                self.runtime.store.expire(now)
            await self.notifications.answer(query_id, tr(language, exc.code), True)
            return
        await self.notifications.answer(query_id, tr(language, "done"))
