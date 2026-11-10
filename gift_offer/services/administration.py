from __future__ import annotations

import hmac
import logging
import re
from html import escape

from .. import admin
from ..models import (
    CallbackQuery,
    Message,
)
from ..telegram import TelegramError, TransportError
from ..views import help_text, language_for, tr
from .connections import ConnectionService
from .context import AdministrationState, Runtime
from .notifications import NotificationService

log = logging.getLogger("gift_offer.service")


class AdministrationService:
    """Handle private commands, administrator binding and recipient configuration."""

    def __init__(
        self, runtime: Runtime, connections: ConnectionService, notifications: NotificationService
    ) -> None:
        self.runtime = runtime
        self.connections = connections
        self.notifications = notifications
        self.state = AdministrationState()

    async def private_message(self, message: Message, now: int) -> None:
        if message.get("chat", {}).get("type") != "private" or message.get("from", {}).get(
            "is_bot"
        ):
            return
        user = message.get("from", {})
        user_id = user.get("id")
        chat_id = message["chat"]["id"]
        if not isinstance(user_id, int) or chat_id != user_id:
            return
        text = message.get("text", "").strip()
        if not text:
            return
        command = text.split()[0].split("@")[0].lower()
        language = language_for(user, self.runtime.config.default_language)
        self.runtime.store.touch_user(user, now)

        # Handle recipient configuration entered by the administrator.
        if user_id in self.state.recipient_waiting:
            if not self.runtime.store.is_admin(user_id):
                self.state.recipient_waiting.discard(user_id)
                return

            if text.lower() == "/cancel":
                self.state.recipient_waiting.discard(user_id)
                await self.notifications.direct(chat_id, "Настройка получателя отменена.")
                return

            username = text.strip()
            if username.startswith("@"):
                username = username[1:]

            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", username):
                await self.notifications.direct(
                    chat_id,
                    "Некорректный username. Отправь аккаунт в формате @username "
                    "или напиши /cancel для отмены.",
                )
                return

            self.runtime.store.set_setting("gift_recipient_username", username)
            self.runtime.store.mark_all_cards_dirty()
            self.state.recipient_waiting.discard(user_id)

            await self.notifications.direct(
                chat_id,
                f"Получатель сохранён: <b>@{escape(username)}</b>.\n"
                "Теперь этот аккаунт будет использоваться как настроенный получатель.",
            )
            return

        if command == "/start":
            self.runtime.store.record_visit(user, chat_id, message["message_id"], now)
        # No message body, token or promo is written to the audit log.
        if hmac.compare_digest(
            command.encode(), self.runtime.config.admin_promo.casefold().encode()
        ):
            if message.get("forward_origin") or len(text.split()) != 1:
                return
            await self.activate_admin(user_id, chat_id, message["message_id"], now)
            return
        if command in {"/admin", "/logs", "/visits", "/transfers"}:
            if not self.runtime.store.is_admin(user_id):
                await self.notifications.direct(
                    chat_id, "Этот раздел доступен только администратору."
                )
                return
            category = {
                "/admin": "home",
                "/logs": "all",
                "/visits": "visits",
                "/transfers": "transfers",
            }[command]
            await self.show_admin(chat_id, category)
            return
        if command in {"/start", "/help", ".buy"}:
            response = help_text(self.runtime.config.name, language)
        elif command in {"/terms", "/paysupport"}:
            response = tr(language, "terms", name=escape(self.runtime.config.name))
        else:
            return
        try:
            await self.notifications.direct(chat_id, response)
        except (TelegramError, TransportError) as exc:
            log.warning("Private reply failed: %s", exc)

    async def activate_admin(self, user_id: int, chat_id: int, message_id: int, now: int) -> None:
        if self.runtime.store.is_admin(user_id):
            await self.show_admin(chat_id)
            return
        if self.runtime.store.get_setting("admin_user", ""):
            await self.notifications.direct(
                chat_id, "Админ-доступ уже закреплён за другим аккаунтом."
            )
            return
        connected = False
        for stored in self.runtime.store.known_connections(user_id):
            try:
                current = await self.connections.connection(stored["id"])
            except (TelegramError, TransportError):
                continue
            if current.get("is_enabled") and current.get("user", {}).get("id") == user_id:
                connected = True
                break
        if not connected:
            await self.notifications.direct(
                chat_id,
                "Сначала подключи бота к своему аккаунту через настройки чат-ботов Telegram, затем повтори промокод.",
            )
            return
        if self.runtime.store.claim_admin(user_id, now, f"admin:{chat_id}:{message_id}"):
            await self.show_admin(chat_id)
        else:
            await self.notifications.direct(
                chat_id, "Админ-доступ уже закреплён за другим аккаунтом."
            )

    async def show_admin(
        self, chat_id: int, category: str = "home", before: int = 0, message_id: int | None = None
    ) -> None:
        # Every entry point, including direct calls and callbacks, verifies the same binding.
        if not self.runtime.store.is_admin(chat_id):
            return
        if category == "home":
            text, markup = admin.home(self.runtime.store.audit_stats())
        else:
            text, markup = admin.log_page(
                category, self.runtime.store.audit_page(category, before, admin.PAGE_SIZE + 1)
            )
        if message_id is None:
            await self.notifications.direct(chat_id, text, markup)
        else:
            try:
                await self.runtime.api.call(
                    "editMessageText",
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=markup,
                    link_preview_options={"is_disabled": True},
                )
            except TelegramError as exc:
                if "message is not modified" not in exc.description.lower():
                    log.warning("Admin view could not be updated: %s", exc)
            except TransportError as exc:
                log.warning("Admin view could not be updated: %s", exc)

    async def admin_callback(self, query: CallbackQuery) -> None:
        user, message = query.get("from", {}), query.get("message", {})
        user_id = user.get("id")
        if (
            not self.runtime.store.is_admin(user_id)
            or user.get("is_bot")
            or message.get("chat", {}).get("type") != "private"
            or message.get("chat", {}).get("id") != user_id
            or message.get("from", {}).get("id") != self.runtime.bot_id
            or message.get("business_connection_id")
            or message.get("forward_origin")
            or not message.get("message_id")
            or message.get("date") == 0
        ):
            await self.notifications.answer(
                query["id"], "Админ-доступ недоступен для этой кнопки.", True
            )
            return
        data = query.get("data", "")

        if data == "a:recipient":
            current = self.runtime.store.get_setting("gift_recipient_username", "")
            self.state.recipient_waiting.add(user_id)
            await self.notifications.answer(query["id"], "")

            current_text = (
                f"Текущий получатель: <b>@{escape(current)}</b>\n\n"
                if current
                else "Получатель пока не установлен.\n\n"
            )

            await self.notifications.direct(
                user_id,
                current_text + "Отправь username аккаунта, на который должны направляться "
                "подарки.\n\n"
                "Например: @username\n\n"
                "Для отмены напиши /cancel.",
            )
            return

        if data == "a:home":
            category, before = "home", 0
        else:
            match = re.fullmatch(r"a:(all|visits|transfers|offers):([0-9]{1,19})", data)
            if not match:
                await self.notifications.answer(query["id"], "Неизвестная кнопка.", True)
                return
            category, cursor = match.groups()
            before = int(cursor)
            if before >= 2**63:
                await self.notifications.answer(query["id"], "Неизвестная страница.", True)
                return
        await self.notifications.answer(query["id"], "")
        await self.show_admin(user_id, category, before, message["message_id"])
