from __future__ import annotations

import logging

from ..models import (
    BusinessConnection,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
)
from ..telegram import TelegramError, TransportError
from ..views import tr
from .context import Runtime

log = logging.getLogger("gift_offer.service")


class NotificationService:
    """Send direct replies, callback answers and optional topic notifications."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    async def answer(self, query_id: str, text: str, alert: bool = False) -> None:
        try:
            await self.runtime.api.call(
                "answerCallbackQuery", callback_query_id=query_id, text=text[:200], show_alert=alert
            )
        except (TelegramError, TransportError) as exc:
            log.warning("Callback answer failed: %s", exc)

    async def direct(
        self, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None
    ) -> None:
        extras = {"reply_markup": reply_markup} if reply_markup is not None else {}
        await self.runtime.api.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
            **extras,
        )

    async def notify_owner(self, connection: BusinessConnection, text: str) -> None:
        try:
            await self.direct(connection["user_chat_id"], text)
        except (TelegramError, TransportError) as exc:
            log.warning("Owner notification unavailable: %s", exc)

    async def notify_topic(
        self, thread_id: int, text: str, *, link_preview_options: LinkPreviewOptions | None = None
    ) -> None:
        if not self.runtime.config.event_chat_id or not thread_id:
            return
        try:
            await self.runtime.api.call(
                "sendMessage",
                chat_id=self.runtime.config.event_chat_id,
                message_thread_id=thread_id,
                text=text,
                parse_mode="HTML",
                link_preview_options=link_preview_options or {"is_disabled": True},
            )
        except (TelegramError, TransportError) as exc:
            log.warning("Notification for topic %s could not be confirmed: %s", thread_id, exc)

    def peer_flood_details(self, language: str, connection_id: str, chat_id: int, now: int) -> str:
        incoming = self.runtime.store.last_incoming(connection_id, chat_id)
        key = (
            "incoming_unknown"
            if not incoming
            else "incoming_recent"
            if now - incoming < 86400
            else "incoming_old"
        )
        return tr(language, key)
