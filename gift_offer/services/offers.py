from __future__ import annotations

import logging
from html import escape

from ..domain import OfferError, parse_buy
from ..models import (
    BusinessConnection,
    Message,
)
from ..telegram import TelegramError, TransportError
from ..views import card_payload, help_text, render_text, tr
from .common import can_reply, worker_label
from .connections import ConnectionService
from .context import Runtime
from .gifts import GiftService
from .notifications import NotificationService

log = logging.getLogger("gift_offer.service")


class OfferService:
    """Create offers and handle owner commands in business chats."""

    def __init__(
        self,
        runtime: Runtime,
        connections: ConnectionService,
        gifts: GiftService,
        notifications: NotificationService,
    ) -> None:
        self.runtime = runtime
        self.connections = connections
        self.gifts = gifts
        self.notifications = notifications

    async def business_message(self, message: Message, now: int) -> None:
        # Observe genuine incoming updates even when they are not commands.
        # Edited messages and callback clicks do not refresh the reply window.
        sender, chat = message.get("from", {}), message.get("chat", {})
        connection_id, at = message.get("business_connection_id"), message.get("date")
        if (
            isinstance(connection_id, str)
            and connection_id
            and chat.get("type") == "private"
            and type(chat.get("id")) is int
            and chat["id"] > 0
            and sender.get("id") == chat["id"]
            and not sender.get("is_bot")
            and not message.get("sender_business_bot")
            and not message.get("is_from_offline")
            and type(at) is int
            and 0 < at <= now
        ):
            self.runtime.store.note_incoming(connection_id, chat["id"], at)
        text = message.get("text", "").strip()
        is_gift = message.get("unique_gift", {}).get("origin") == "transfer"
        if not is_gift and (
            not text or text.split()[0].lower() not in {".buy", ".help", ".status", ".cancel"}
        ):
            return
        sender = message.get("from", {})
        if (
            not isinstance(sender.get("id"), int)
            or sender.get("id", 0) <= 0
            or sender.get("is_bot")
        ):
            return
        if message.get("forward_origin") or (message.get("sender_business_bot") and not is_gift):
            return
        chat = message.get("chat", {})
        if chat.get("type") != "private" or not isinstance(chat.get("id"), int) or chat["id"] <= 0:
            return
        if chat["id"] == sender["id"] and not is_gift:
            return
        connection_id = message.get("business_connection_id")
        if not connection_id:
            return
        try:
            connection = await self.connections.connection(connection_id)
        except (TelegramError, TransportError) as exc:
            log.warning("Connection lookup failed: %s", exc)
            if isinstance(exc, TransportError) or exc.code == 429 or exc.code >= 500:
                raise
            return
        owner_id = connection["user"]["id"]
        self.runtime.store.touch_user(connection["user"], now)
        self.runtime.store.touch_user(sender, now)
        self.runtime.store.touch_user(chat, now)
        if is_gift:
            if connection.get("is_enabled"):
                await self.gifts.telegram_gift(message, connection, now)
            return
        if owner_id != sender["id"]:
            return
        language = self.runtime.config.default_language
        if not can_reply(connection):
            await self.notifications.notify_owner(connection, tr(language, "connection_disabled"))
            return
        command = text.split()[0].lower()
        if command == ".help":
            await self.notifications.notify_owner(
                connection, help_text(self.runtime.config.name, language)
            )
            return
        if command in {".status", ".cancel"}:
            await self.owner_command(connection, message, command, now)
            return
        if self.runtime.store.from_source(connection_id, chat["id"], message["message_id"]):
            return
        try:
            spec = parse_buy(text, language)
        except OfferError as exc:
            await self.notifications.notify_owner(connection, tr(language, exc.code))
            return
        if now - self.runtime.store.latest_creation(sender["id"]) < self.runtime.config.cooldown:
            await self.notifications.notify_owner(connection, tr(language, "cooldown"))
            return
        offer = self.runtime.store.create(
            spec, connection, message, now, self.runtime.config.offer_ttl
        )
        try:
            card = card_payload(
                offer,
                self.runtime.config.name,
                recipient_username=self.runtime.store.get_setting("gift_recipient_username", ""),
                now=now,
            )
            result = await self.runtime.api.call("sendMessage", **card)
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if type(message_id) is not int or message_id <= 0:
                raise TransportError("Telegram did not return a delivered message identifier")
        except (TelegramError, TransportError) as exc:
            ambiguous = isinstance(exc, TransportError) or exc.code >= 500
            self.runtime.store.delivery_problem(offer["id"], ambiguous, now)
            log.warning("Offer %s could not be delivered: %s", offer["id"], exc)
            if ambiguous:
                response = tr(spec.language, "check_chat")
            else:
                key = "peer_flood" if exc.is_peer_flood else "delivery_failed"
                if exc.is_peer_flood:
                    self.runtime.store.pause_card_updates(connection_id, chat["id"])
                cause = escape(
                    exc.description.replace(self.runtime.config.token, "[BOT_TOKEN]")[:500]
                )
                response = (
                    tr(spec.language, key)
                    + "\n\n"
                    + tr(spec.language, "delivery_cause", code=exc.code, cause=cause)
                )
                if exc.is_peer_flood:
                    response += "\n\n" + self.notifications.peer_flood_details(
                        spec.language, connection_id, chat["id"], int(self.runtime.clock())
                    )
            await self.notifications.notify_owner(connection, response)
            return
        self.runtime.store.attach_card(offer["id"], message_id)
        self.runtime.store.resume_card_updates(connection_id, chat["id"])
        await self.notifications.notify_topic(
            self.runtime.config.new_offer_thread_id,
            "⚡ <b>новый Лог!</b>\n\n"
            f"👤 Работник: {worker_label(chat)}\n"
            f"🤝 Покупатель: {escape(offer['buyer_name'])}\n"
            f"├ предмет: {escape(spec.gift_url)}\n"
            "└ Сервис: оффери",
            link_preview_options=card["link_preview_options"],
        )

    async def owner_command(
        self, connection: BusinessConnection, message: Message, command: str, now: int
    ) -> None:
        parts = message["text"].strip().split()
        language = self.runtime.config.default_language
        if len(parts) != 1:
            await self.notifications.notify_owner(connection, tr(language, "status_syntax"))
            return
        offer = self.runtime.store.latest_offer(connection["id"], message["chat"]["id"])
        if not offer:
            await self.notifications.notify_owner(connection, tr(language, "not_found"))
            return
        if command == ".cancel":
            try:
                offer = self.runtime.store.transition(
                    offer["id"], "cancel", connection["user"]["id"], now
                )
            except OfferError as exc:
                await self.notifications.notify_owner(connection, tr(offer["language"], exc.code))
                return
        await self.notifications.notify_owner(
            connection, render_text(offer, self.runtime.config.name, now=now)
        )
