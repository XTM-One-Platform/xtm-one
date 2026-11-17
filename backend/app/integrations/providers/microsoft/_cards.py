"""Adaptive Cards: the ``card`` argument of the Teams message tools.

Graph posts a card as an attachment of the chatMessage: an ``attachments``
entry holding the serialized card, and an ``<attachment id="...">`` tag in the
HTML body at the place the card shows. The tools take the card as data and
write both: the tag goes in after the Markdown conversion, whose sanitizer
would strip it.

A message posted as the signed-in account has no bot behind it, so Teams runs
only the ``Action.OpenUrl`` card action; every other action and every input
would render and then do nothing. Such a card is refused before any request,
with the Teams bot card tools named, since a bot can receive what a card
submits.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from app.services.teams_format import ensure_full_width_card

ADAPTIVE_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"

#: A Teams message holds about 28 KB; a larger message with its card is
#: refused before posting. Counted by :func:`message_bytes`.
MAX_CARD_BYTES = 28 * 1024

#: The one card action Teams runs without a bot.
_ACTIONS_WITHOUT_BOT = frozenset({"Action.OpenUrl"})

_BOT_TOOLS = "send_teams_bot_channel_card, send_teams_bot_chat_card or send_teams_bot_group_chat_card"


def card_schema() -> dict[str, Any]:
    """The ``card`` property of a message tool's input schema."""
    return {
        "type": "object",
        "description": (
            'One Adaptive Card to post with the message: the card JSON, with "type": '
            '"AdaptiveCard", a "version" and a "body". The message text, when given, '
            "shows above the card; message can be left out. Only the Action.OpenUrl "
            "action works in a message posted as the signed-in user: a card with inputs "
            f"or other actions needs a bot ({_BOT_TOOLS})."
        ),
    }


def _bot_only_types(node: Any, found: set[str]) -> None:
    if isinstance(node, Mapping):
        kind = node.get("type")
        if isinstance(kind, str) and (
            kind.startswith("Input.") or (kind.startswith("Action.") and kind not in _ACTIONS_WITHOUT_BOT)
        ):
            found.add(kind)
        for value in node.values():
            _bot_only_types(value, found)
    elif isinstance(node, list):
        for item in node:
            _bot_only_types(item, found)


def parse_card(raw: Any, *, full_width: bool = False) -> tuple[dict | None, str | None]:
    """Read a tool's ``card`` argument, before any request is made.

    Returns ``(card, error)``; ``(None, None)`` when no card was given. A
    channel card (*full_width*) opts into full-width rendering unless it sets
    its own ``msteams.width``.
    """
    if raw is None or raw == "" or raw == {}:
        return None, None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None, "Error: card is not valid JSON. Nothing was posted."
        if raw == {}:
            return None, None
    if not isinstance(raw, Mapping):
        return None, "Error: card must be one Adaptive Card object. Nothing was posted."
    if raw.get("type") != "AdaptiveCard":
        return None, (
            f"Error: card has type {str(raw.get('type'))[:40]!r}; an Adaptive Card has "
            '"type": "AdaptiveCard". Nothing was posted.'
        )
    found: set[str] = set()
    _bot_only_types(raw, found)
    if found:
        return None, (
            f"Error: the card holds {', '.join(sorted(found))}, which Teams runs only for "
            "a bot; a message posted as the signed-in user supports Action.OpenUrl alone. "
            f"Remove them, or post the card with {_BOT_TOOLS}. Nothing was posted."
        )
    card = dict(raw)
    if full_width:
        card = ensure_full_width_card(card)
    return card, None


def message_bytes(payload: dict) -> int:
    """The UTF-8 size of what Teams stores for the Graph chatMessage *payload*.

    The HTML body plus each attachment's content as sent: the serialized card
    counted once. Measuring the whole request would escape every quote of the
    card a second time (a JSON string inside JSON) and refuse a card Teams
    would keep.
    """
    size = len(str(payload["body"].get("content") or "").encode())
    for item in payload.get("attachments") or ():
        size += len(str(item.get("content") or "").encode())
    return size


def attach_card(payload: dict, card: dict) -> str | None:
    """Add *card* to the Graph chatMessage *payload*, below its body.

    Returns an error, and nothing is to be sent, when the message with its
    card is larger than a Teams message.
    """
    attachment_id = uuid.uuid4().hex
    body = payload["body"]
    body["content"] = f'{body.get("content") or ""}<attachment id="{attachment_id}"></attachment>'
    payload["attachments"] = [
        {
            "id": attachment_id,
            "contentType": ADAPTIVE_CARD_CONTENT_TYPE,
            "content": json.dumps(card, ensure_ascii=False),
        }
    ]
    size = message_bytes(payload)
    if size > MAX_CARD_BYTES:
        return (
            f"Error: the message with its card is {size} bytes; a Teams message holds at most "
            f"{MAX_CARD_BYTES}. Nothing was posted."
        )
    return None


def _sent_card_id(payload: dict) -> str | None:
    for item in payload.get("attachments") or ():
        if item.get("contentType") == ADAPTIVE_CARD_CONTENT_TYPE:
            return item.get("id")
    return None


def card_sent_note(payload: dict) -> str:
    """The card an edit sent: Graph answers an edit with no message to read."""
    return " Adaptive Card sent." if _sent_card_id(payload) else ""


def card_note(payload: dict, msg: Any, read_back_tool: str) -> str:
    """What a post adds about the card of *payload*, as the message Graph returned holds it."""
    attachment_id = _sent_card_id(payload)
    if attachment_id is None:
        return ""
    attachments = msg.get("attachments") if isinstance(msg, dict) else None
    if not isinstance(attachments, list):
        return f" Adaptive Card sent; Teams did not return it, read the message back with {read_back_tool} to check."
    if any(isinstance(item, dict) and item.get("id") == attachment_id for item in attachments):
        return " Adaptive Card attached."
    return " Warning: Teams did not keep the Adaptive Card; the message shows without it."


def nothing_to_post(message: Any, card: dict | None, mentions: list) -> str | None:
    """The error of a call that gives no text, no card and no mention."""
    if card is None and not mentions and not (isinstance(message, str) and message.strip()):
        return "Error: pass a message, a card, or both. Nothing was posted."
    return None
