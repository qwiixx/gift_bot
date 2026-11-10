from __future__ import annotations

import logging
from html import escape

from ..models import (
    BusinessConnection,
    Offer,
)
from ..telegram import TelegramError, TransportError
from ..views import card_payload, tr
from .common import can_reply
from .connections import ConnectionService
from .context import CardState, Runtime
from .notifications import NotificationService

log = logging.getLogger("gift_offer.service")


class CardService:
    """Refresh cards and clean up creation commands with the existing retry policy."""

    def __init__(
        self, runtime: Runtime, connections: ConnectionService, notifications: NotificationService
    ) -> None:
        self.runtime = runtime
        self.connections = connections
        self.notifications = notifications
        self.state = CardState()

    async def _cleanup_commands(self, now: int) -> None:
        # Jobs exist only for new commands. A confirmed card is required;
        # failed sends are never retried or cleaned up by this queue.
        for offer in self.runtime.store.command_cleanups(now):
            if now - offer["sent_at"] >= 48 * 3600:
                self.runtime.store.finish_command_cleanup(offer["id"])
                continue
            try:
                connection = await self.connections.connection(offer["connection_id"])
                if (
                    not connection.get("is_enabled")
                    or connection["user"]["id"] != offer["buyer_id"]
                ):
                    self.runtime.store.defer_command_cleanup(offer["id"], now + 60)
                    continue
                # The owner sent .buy, so deleting just the bot's own messages
                # is insufficient. Keep the job until rights are refreshed.
                if not connection.get("rights", {}).get("can_delete_all_messages"):
                    self.runtime.store.defer_command_cleanup(offer["id"], now + 60)
                    if connection["id"] not in self.connections.state.delete_notices:
                        self.connections.state.delete_notices.add(connection["id"])
                        await self.notifications.notify_owner(
                            connection, tr(offer["language"], "delete_permission")
                        )
                    continue
                deleted = await self.runtime.api.call(
                    "deleteBusinessMessages",
                    business_connection_id=connection["id"],
                    message_ids=[offer["source_message_id"]],
                )
                if deleted is not True:
                    raise TransportError("Telegram did not confirm command deletion")
            except TelegramError as exc:
                description = exc.description.lower()
                if "message to delete not found" in description:
                    self.runtime.store.finish_command_cleanup(offer["id"])
                elif exc.code == 429 or exc.code >= 500:
                    self.runtime.store.defer_command_cleanup(
                        offer["id"], now + max(exc.retry_after, 30)
                    )
                    log.info("Command deletion deferred after a temporary Telegram error.")
                elif exc.code == 403:
                    self.connections.state.connections.pop(offer["connection_id"], None)
                    self.runtime.store.defer_command_cleanup(offer["id"], now + 60)
                else:
                    self.runtime.store.finish_command_cleanup(offer["id"])
                    log.warning("Creation command could not be deleted: %s", exc)
                continue
            except TransportError:
                self.runtime.store.defer_command_cleanup(offer["id"], now + 30)
                log.info("Command deletion deferred while Telegram is unreachable.")
                continue
            self.runtime.store.finish_command_cleanup(offer["id"])

    async def refresh_paused_card(
        self, offer: Offer, connection: BusinessConnection, query_id: str, now: int
    ) -> bool:
        # Called only by a verified participant's button on this delivered card.
        # Never turn a rejected creation command or an arbitrary message into a card.
        wait = self.runtime.store.card_update_wait(offer["connection_id"], offer["chat_id"], now)
        if wait:
            await self.notifications.answer(
                query_id, tr(offer["language"], "card_wait", seconds=wait), True
            )
            return False
        try:
            result = await self.runtime.api.call(
                "editMessageText",
                message_id=offer["card_message_id"],
                **card_payload(
                    offer,
                    self.runtime.config.name,
                    recipient_username=self.runtime.store.get_setting(
                        "gift_recipient_username", ""
                    ),
                    now=now,
                ),
            )
            if (
                not isinstance(result, dict)
                or type(result.get("message_id")) is not int
                or result["message_id"] != offer["card_message_id"]
            ):
                raise TransportError("Telegram did not confirm the refreshed card identifier")
        except TelegramError as exc:
            if not (
                exc.code == 400
                and "message is not modified" in exc.description.lower()
                and not exc.is_peer_flood
            ):
                if exc.code == 429:
                    self.runtime.store.defer_card_updates(
                        offer["connection_id"],
                        offer["chat_id"],
                        int(self.runtime.clock()) + max(exc.retry_after, 60),
                    )
                self.runtime.store.pause_card_updates(offer["connection_id"], offer["chat_id"])
                log.warning("Requested refresh of card %s was rejected: %s", offer["id"], exc)
                await self.notifications.answer(
                    query_id, tr(offer["language"], "card_updates_paused"), True
                )
                cause = escape(
                    exc.description.replace(self.runtime.config.token, "[BOT_TOKEN]")[:500]
                )
                response = (
                    tr(offer["language"], "card_refresh_failed")
                    + "\n\n"
                    + tr(offer["language"], "delivery_cause", code=exc.code, cause=cause)
                )
                await self.notifications.notify_owner(connection, response)
                return False
        except TransportError as exc:
            log.warning("Requested refresh of card %s was not confirmed: %s", offer["id"], exc)
            await self.notifications.answer(
                query_id, tr(offer["language"], "card_refresh_unknown"), True
            )
            return False
        self.runtime.store.mark_clean(offer["id"], offer["status"])
        self.runtime.store.resume_card_updates(offer["connection_id"], offer["chat_id"])
        self.state.retry_at.pop(offer["id"], None)
        return True

    async def _flush(self, now: int) -> None:
        for offer in self.runtime.store.dirty_offers(now):
            if self.state.retry_at.get(offer["id"], 0) > now:
                continue
            # Recheck because a preceding card in this batch may have paused the chat.
            if not self.runtime.store.can_update_card(
                offer["connection_id"], offer["chat_id"], now
            ):
                continue
            try:
                connection = await self.connections.connection(offer["connection_id"])
                if not can_reply(connection):
                    continue
                await self.runtime.api.call(
                    "editMessageText",
                    message_id=offer["card_message_id"],
                    **card_payload(
                        offer,
                        self.runtime.config.name,
                        recipient_username=self.runtime.store.get_setting(
                            "gift_recipient_username", ""
                        ),
                        now=now,
                    ),
                )
            except TelegramError as exc:
                if exc.is_peer_flood:
                    self.runtime.store.pause_card_updates(offer["connection_id"], offer["chat_id"])
                    log.warning(
                        "Card updates paused for chat %s on connection %s: %s",
                        offer["chat_id"],
                        offer["connection_id"],
                        exc,
                    )
                    connection = self.connections.state.connections.get(offer["connection_id"])
                    if connection:
                        cause = escape(
                            exc.description.replace(self.runtime.config.token, "[BOT_TOKEN]")[:500]
                        )
                        response = (
                            tr(offer["language"], "card_peer_flood")
                            + "\n\n"
                            + tr(offer["language"], "delivery_cause", code=exc.code, cause=cause)
                        )
                        response += "\n\n" + self.notifications.peer_flood_details(
                            offer["language"],
                            offer["connection_id"],
                            offer["chat_id"],
                            int(self.runtime.clock()),
                        )
                        await self.notifications.notify_owner(connection, response)
                elif exc.code == 429:
                    self.runtime.store.defer_card_updates(
                        offer["connection_id"],
                        offer["chat_id"],
                        int(self.runtime.clock()) + max(exc.retry_after, 60),
                    )
                    log.info("Card updates deferred for the Telegram retry_after interval.")
                elif "message is not modified" in exc.description.lower():
                    self.runtime.store.mark_clean(offer["id"], offer["status"])
                elif "message to edit not found" in exc.description.lower():
                    self.runtime.store.hide_card(
                        offer["connection_id"], offer["chat_id"], offer["card_message_id"], now
                    )
                else:
                    log.warning("Card update for %s failed: %s", offer["id"], exc)
                    self.connections.state.connections.pop(offer["connection_id"], None)
                    self.state.retry_at[offer["id"]] = now + 60
                continue
            except TransportError as exc:
                log.warning("Card update for %s failed: %s", offer["id"], exc)
                self.state.retry_at[offer["id"]] = now + 30
                continue
            self.runtime.store.mark_clean(offer["id"], offer["status"])
            self.state.retry_at.pop(offer["id"], None)
