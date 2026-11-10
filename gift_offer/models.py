"""Typed mapping contracts; no coercion, defaults or runtime validation.

Telegram payloads remain dictionaries so absent keys, unknown API fields,
identity and existing defensive checks keep their original semantics.
"""

from typing import NotRequired, TypedDict


class TelegramUser(TypedDict, total=False):
    id: int
    is_bot: bool
    first_name: str
    last_name: str
    username: str
    language_code: str


class Chat(TelegramUser, total=False):
    type: str


class BusinessRights(TypedDict, total=False):
    can_reply: bool
    can_delete_all_messages: bool


class BusinessConnection(TypedDict, total=False):
    id: str
    user: TelegramUser
    user_chat_id: int
    date: int
    is_enabled: bool
    rights: BusinessRights


class Gift(TypedDict, total=False):
    name: str


class UniqueGift(TypedDict, total=False):
    origin: str
    gift: Gift
    owned_gift_id: str


# Functional syntax preserves Telegram's reserved key "from".
Message = TypedDict(
    "Message",
    {
        "message_id": int,
        "date": int,
        "chat": Chat,
        "from": TelegramUser,
        "text": str,
        "business_connection_id": str,
        "sender_business_bot": TelegramUser,
        "is_from_offline": bool,
        "forward_origin": object,
        "unique_gift": UniqueGift,
    },
    total=False,
)

CallbackQuery = TypedDict(
    "CallbackQuery",
    {"id": str, "from": TelegramUser, "message": Message, "data": str},
    total=False,
)


class DeletedMessages(TypedDict, total=False):
    business_connection_id: str
    chat: Chat
    message_ids: list[int]


class Update(TypedDict, total=False):
    update_id: int
    business_connection: BusinessConnection
    callback_query: CallbackQuery
    business_message: Message
    deleted_business_messages: DeletedMessages
    message: Message


class OfferDraft(TypedDict):
    """Columns inserted before SQLite supplies its existing defaults."""

    id: str
    connection_id: str
    chat_id: int
    source_message_id: int
    buyer_id: int
    seller_id: int
    buyer_name: str
    buyer_username: str
    gift_url: str
    gift_slug: str
    gift_name: str
    amount: str
    currency: str
    language: str
    status: str
    created_at: int
    expires_at: int
    updated_at: int


class Offer(OfferDraft):
    """Complete offers table row; integer flags retain their SQLite values."""

    card_message_id: int | None
    payment_claimed: int
    dirty: int
    display_enabled: int


class CommandCleanup(Offer):
    sent_at: int


class AuditEntry(TypedDict):
    id: int
    source_key: str
    kind: str
    at: int
    actor_id: int | None
    recipient_id: int | None
    owner_id: int | None
    gift_url: str
    gift_name: str
    amount: str
    currency: str
    evidence: str
    actor_first: str | None
    actor_last: str | None
    actor_username: str | None
    recipient_first: str | None
    recipient_last: str | None
    recipient_username: str | None


class InlineButton(TypedDict):
    text: str
    callback_data: NotRequired[str]
    url: NotRequired[str]
    icon_custom_emoji_id: NotRequired[str]


class InlineKeyboardMarkup(TypedDict):
    inline_keyboard: list[list[InlineButton]]


class LinkPreviewOptions(TypedDict, total=False):
    is_disabled: bool
    url: str
    prefer_large_media: bool
    show_above_text: bool


class CardPayload(TypedDict):
    business_connection_id: str
    chat_id: int
    text: str
    parse_mode: str
    link_preview_options: LinkPreviewOptions
    reply_markup: InlineKeyboardMarkup
