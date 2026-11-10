from __future__ import annotations

import logging
from html import escape

from ..domain import OfferError, parse_buy
from ..models import (
    BusinessConnection,
    Message,
)
from .common import worker_label
from .context import Runtime
from .notifications import NotificationService

log = logging.getLogger("gift_offer.service")


class GiftService:
    """Record Telegram gift-transfer events and notify the configured topic."""

    def __init__(self, runtime: Runtime, notifications: NotificationService) -> None:
        self.runtime = runtime
        self.notifications = notifications

    async def telegram_gift(
        self, message: Message, connection: BusinessConnection, now: int
    ) -> None:
        info = message["unique_gift"]
        gift = info.get("gift", {})
        slug = gift.get("name", "")
        if not isinstance(slug, str):
            return
        try:
            spec = parse_buy(f".buy https://t.me/nft/{slug} 1 stars")
        except OfferError:
            return
        owner_id, sender_id, peer_id = (
            connection["user"]["id"],
            message["from"]["id"],
            message["chat"]["id"],
        )
        # A private service message's sender must be one of the two participants.
        # Do not guess a recipient for ambiguous or forwarded messages.
        if sender_id == owner_id and not info.get("owned_gift_id"):
            recipient_id = peer_id
        elif sender_id == peer_id:
            recipient_id = owner_id
        else:
            return
        recorded = self.runtime.store.record_telegram_transfer(
            connection["id"],
            peer_id,
            message["message_id"],
            int(message.get("date") or now),
            sender_id,
            recipient_id,
            owner_id,
            spec.gift_url,
            spec.gift_name,
        )
        if recorded:
            await self.notifications.notify_topic(
                self.runtime.config.nft_transfer_thread_id,
                "🧠 <b>Найден новый коллекционный предмет!</b>\n\n"
                f"🔲 Название: {escape(spec.gift_name)}\n"
                f"👤 Работник: {worker_label(message['from'])}\n"
                f"🌐 Ссылка: {escape(spec.gift_url)}",
            )
