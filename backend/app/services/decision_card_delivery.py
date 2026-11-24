# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Sending a decision card through the Teams bot (#842, S9; human-gates.md I.2, I.4 path (a)).

The bot can only post where it is a member. Each inbound bot activity records
its conversation (:func:`remember_bot_conversation`): a 1:1, a group chat or a
channel. A sender then asks :func:`bot_conversation` whether the bot is in the
target conversation before the send, and :func:`send_decision_card` posts the
card of :mod:`app.services.decision_cards` there, answerable in place with
``xtm.decision.answer``. When the bot is not there, the caller keeps its own
path (the account's Graph message, answered through the links of slice S9b).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.models.decision import Decision

logger = logging.getLogger(__name__)

_KEY = "teams:bot_conversation:{}"
#: A conversation the bot has not heard from in 90 days is no longer trusted.
_TTL_SECONDS = 60 * 60 * 24 * 90
#: The Bot Framework conversation types a card send distinguishes.
CONVERSATION_TYPES: frozenset[str] = frozenset({"personal", "groupChat", "channel"})
#: The toast preview of the card (``summary``, never ``text``: see ``TeamsChannel``).
_PREVIEW_CHARS = 200


def conversation_key(conversation_id: str) -> str:
    """The registry key of a conversation; a channel thread id names its channel."""
    return _KEY.format(base_conversation_id(conversation_id))


def base_conversation_id(conversation_id: str) -> str:
    # A channel thread id carries ";messageid=<root>": the bot is in the channel.
    return str(conversation_id or "").split(";", 1)[0].strip()


async def remember_bot_conversation(conversation_id: str, service_url: str, conversation_type: str) -> None:
    """Record that the bot is in *conversation_id* (called on every inbound bot activity)."""
    base = base_conversation_id(conversation_id)
    if not base or not service_url:
        return
    from app.core.redis import redis_client

    kind = conversation_type if conversation_type in CONVERSATION_TYPES else ""
    try:
        redis = await redis_client()
        await redis.set(
            conversation_key(base),
            json.dumps({"service_url": service_url, "conversation_id": base, "conversation_type": kind}),
            ex=_TTL_SECONDS,
        )
    except Exception as exc:
        logger.warning("Failed to record the Teams bot conversation: %s", format_exception_message(exc))


async def bot_conversation(conversation_id: str) -> dict[str, str] | None:
    """The bot's reference to *conversation_id*, or ``None`` when the bot is not known there."""
    base = base_conversation_id(conversation_id)
    if not base:
        return None
    from app.core.redis import redis_client

    try:
        redis = await redis_client()
        raw = await redis.get(conversation_key(base))
    except Exception as exc:
        logger.warning("Failed to read the Teams bot conversation: %s", format_exception_message(exc))
        return None
    if not raw:
        return None
    try:
        ref = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(ref, dict) or not ref.get("service_url") or not ref.get("conversation_id"):
        return None
    return {
        "service_url": str(ref["service_url"]),
        "conversation_id": str(ref["conversation_id"]),
        "conversation_type": str(ref.get("conversation_type") or ""),
    }


async def personal_bot_conversation(user_email: str) -> dict[str, str] | None:
    """The person's 1:1 with the bot, or ``None`` when they never wrote to it."""
    if not user_email:
        return None
    from app.channels.teams import get_personal_conversation_reference

    ref = await get_personal_conversation_reference(user_email)
    if not isinstance(ref, dict) or not ref.get("service_url") or not ref.get("conversation_id"):
        return None
    return {
        "service_url": str(ref["service_url"]),
        "conversation_id": str(ref["conversation_id"]),
        "conversation_type": "personal",
    }


async def open_decision(db: AsyncSession, source_type: str, source_id: uuid.UUID | str) -> Decision | None:
    """The open decision of a source (a run, a flow run, a work item), or ``None``."""
    try:
        sid = source_id if isinstance(source_id, uuid.UUID) else uuid.UUID(str(source_id))
    except ValueError:
        return None
    return (
        await db.execute(
            select(Decision)
            .where(Decision.source_type == source_type, Decision.source_id == sid, Decision.status == "open")
            .order_by(Decision.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def card_message(card: dict[str, Any], decision: Decision) -> Any:
    """The outbound message that carries *card* (the question as the toast preview)."""
    from app.channels.base import OutboundMessage

    ask = decision.ask if isinstance(decision.ask, dict) else {}
    preview = " ".join(str(ask.get("question") or "").split())[:_PREVIEW_CHARS]
    return OutboundMessage(text=preview, metadata={"adaptive_card": card})


async def decision_card_for_tool(raw_id: str) -> tuple[dict[str, Any], str] | str:
    """The card and toast preview of the decision *raw_id* names, for a channel send tool.

    *raw_id* is a decision id as the router reads it (``run:<uuid>``,
    ``flow_run:<uuid>``, ``work_item:<uuid>`` or a decisions row id), so an
    agent posts a decision without writing card JSON (I.2). The answer is the
    error text the tool returns when the id names no decision.
    """
    from fastapi import HTTPException

    from app.core.database import async_session
    from app.services.channel_notices import notice_language
    from app.services.decision_cards import load_view, render_decision_card
    from app.services.decision_router import resolve

    language = notice_language()
    try:
        async with async_session() as db:
            _ref, row = await resolve(db, raw_id)
            if row is None:
                return f"Error: no decision found for {raw_id!r}."
            card = render_decision_card(await load_view(db, row, None, language), language)
            preview = card_message(card, row).text
    except HTTPException:
        return f"Error: no decision found for {raw_id!r}; pass its source and id, e.g. run: then the run's UUID."
    return card, preview


async def send_decision_card(
    db: AsyncSession,
    decision: Decision,
    conversation: dict[str, str],
    *,
    viewer: Any,
    language: str,
) -> bool:
    """Post *decision*'s card into *conversation* as the bot; ``True`` when Teams took it.

    The card is rendered for *viewer* (the person it is sent to, ``None`` when
    unknown); every other viewer gets their own view through the card's
    ``refresh``, and an answer is checked again by the router.
    """
    from app.channels.teams import TeamsChannel, _get_teams_creds
    from app.services.decision_cards import load_view, render_decision_card

    creds = await _get_teams_creds()
    if not creds:
        return False
    token, app_id = creds
    card = render_decision_card(await load_view(db, decision, viewer, language), language)
    channel = TeamsChannel(
        conversation["service_url"],
        conversation["conversation_id"],
        token,
        bot_id=app_id,
        conversation_type=conversation.get("conversation_type") or "",
    )
    try:
        sent = await channel.send_message(card_message(card, decision))
    except Exception as exc:
        logger.warning("Teams decision card for %s not sent: %s", decision.public_id, format_exception_message(exc))
        return False
    if sent is None:
        logger.warning("Teams decision card for %s not sent", decision.public_id)
        return False
    logger.info("Teams decision card for %s sent", decision.public_id)
    return True
