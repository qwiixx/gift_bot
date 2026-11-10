from __future__ import annotations

import re
import time
from html import escape

from .models import (
    CardPayload,
    InlineButton,
    InlineKeyboardMarkup,
    Offer,
    TelegramUser,
)
from .translations import TEXT as TEXT
from .ui import button, price_text


def language_for(user: TelegramUser, default: str = "ru") -> str:
    language = str(user.get("language_code", "")).lower().split("-")[0]
    return language if language in TEXT else default


def tr(language: str, key: str, **kwargs) -> str:
    return TEXT.get(language, TEXT["ru"])[key].format(**kwargs)


def help_text(name: str, language: str) -> str:
    return tr(language, "help", name=escape(name), disclosure="").rstrip()


def remaining_minutes(offer: Offer, now: int) -> int:
    return (max(0, offer["expires_at"] - now) + 59) // 60


def transfer_url(offer: Offer, recipient_username: str = "") -> str | None:
    # Only the administrator-configured recipient is allowed.
    username = str(recipient_username).strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", username):
        return None
    return f"tg://send_gift?to={username}"


def render_text(
    offer: Offer,
    name: str,
    *,
    recipient_username: str = "",
    now: int | None = None,
) -> str:
    language = offer["language"]
    gift = f'<a href="{escape(offer["gift_url"], quote=True)}">{escape(offer["gift_name"])}</a>'
    buyer = f'<a href="tg://user?id={offer["buyer_id"]}">{escape(offer["buyer_name"])}</a>'
    price = price_text(offer["amount"], offer["currency"], label=tr(language, offer["currency"]))

    lines = (
        [
            f"🎁 Подарок {gift} успешно продан!",
            "",
            f"• Передайте {gift} покупателю для зачисления оплаты: {price}",
        ]
        if offer["status"] == "accepted"
        else [tr(language, "proposes", buyer=buyer, price=price, gift=gift)]
    )

    if offer["status"] == "pending":
        hours, minutes = divmod(
            remaining_minutes(offer, int(time.time()) if now is None else now), 60
        )
        lines.extend(["", tr(language, "expires", hours=hours, minutes=minutes)])
    elif offer["status"] != "accepted":
        lines.extend(["", tr(language, offer["status"], buyer=buyer, recipient="")])

    if offer["status"] == "gift_reported" and offer.get("payment_claimed"):
        lines.extend(["", tr(language, "payment_note")])

    return "\n".join(lines)


def keyboard(offer: Offer, recipient_username: str = "") -> InlineKeyboardMarkup:
    language = offer["language"]

    def action(key: str) -> InlineButton:
        return button(
            tr(language, key),
            callback_data=f"g:{offer['id']}:{key}",
            negative=key in {"reject", "cancel"},
        )

    rows: list[list[InlineButton]] = []
    state = offer["status"]

    if state == "pending":
        rows.append([action("reject"), action("accept")])
    elif state in {"accepted", "payment_reported", "gift_reported", "gift_received"}:
        url = transfer_url(offer, recipient_username)
        if url:
            rows.append([button(tr(language, "transfer"), url=url)])
        rows.append([action("confirm_transfer")])

    return {"inline_keyboard": rows}


def card_payload(
    offer: Offer,
    name: str,
    *,
    recipient_username: str = "",
    now: int | None = None,
) -> CardPayload:
    return {
        "business_connection_id": offer["connection_id"],
        "chat_id": offer["chat_id"],
        "text": render_text(offer, name, recipient_username=recipient_username, now=now),
        "parse_mode": "HTML",
        "link_preview_options": {
            "is_disabled": False,
            "url": offer["gift_url"],
            "prefer_large_media": True,
            "show_above_text": False,
        },
        "reply_markup": keyboard(offer, recipient_username),
    }
