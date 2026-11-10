from __future__ import annotations

import re
from html import escape

from ..models import (
    BusinessConnection,
    TelegramUser,
)

CALLBACK = re.compile(
    r"g:([a-f0-9]{16}):(accept|reject|confirm_transfer|cancel|sent|received|settled)"
)


def can_reply(connection: BusinessConnection) -> bool:
    return bool(connection.get("is_enabled") and connection.get("rights", {}).get("can_reply"))


def worker_label(user: TelegramUser) -> str:
    username = user.get("username") or ""
    if re.fullmatch(r"[A-Za-z0-9_]{1,32}", username):
        return f'<a href="https://t.me/{username}">@{username}</a>'
    name = " ".join(filter(None, (user.get("first_name"), user.get("last_name"))))
    return escape(name or "Не указан")
