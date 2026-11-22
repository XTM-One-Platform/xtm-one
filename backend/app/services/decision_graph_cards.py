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

"""Decision cards sent through a Microsoft integration (#842, S9b; human-gates.md I.4 paths (b), (c)).

A message posted as a user, a service account or an agent account carries
the link card of :mod:`app.services.decision_answer_links` (``Action.OpenUrl``
alone). The send is a tool call of the sending agent, under its bindings,
scope, deny list and approval gate (``work_item_notify.agent_graph_caller``),
and leaves a ``notified`` event naming the message it posted.

Once the decision is no longer open, :func:`update_sent_cards` replaces each
of those messages with the decided card, as their sender (``PATCH`` of the
chat or channel message through ``update_microsoft_chat_message`` /
``update_microsoft_channel_message``). When Graph refuses the edit the
message keeps its links: the link page they open shows the decided state and
answers nothing, which is the stated fallback of I.4.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from types import SimpleNamespace
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.models.decision import Decision, DecisionEvent
from app.services import decision_answer_links as answer_links
from app.services import work_item_notify as notify

logger = logging.getLogger(__name__)

#: The send and edit tools of each kind of target.
_SEND = {"chat": "send_microsoft_chat_message", "channel": "send_microsoft_channel_message"}
_UPDATE = {"chat": "update_microsoft_chat_message", "channel": "update_microsoft_channel_message"}
#: The surface a link minted for each kind of target records on its answer.
_SURFACE = {"chat": "teams_chat", "channel": "teams_channel"}
_MESSAGE_ID = re.compile(r"Message ID: ([^\s.]+)")
#: The ``notified`` events of this module carry this ``via``.
VIA = "graph_link_card"

#: ``(sender, integration) -> async context of (tool, arguments) -> (sent, answer)``; tests replace it.
CallerFactory = Callable[[Any, str], Any]


def target_kind(target: Mapping[str, Any]) -> Literal["chat", "channel"]:
    """``channel`` for ``{team_id, channel_id}``, ``chat`` for ``{chat_id}``; anything else is refused."""
    if target.get("team_id") and target.get("channel_id"):
        return "channel"
    if target.get("chat_id"):
        return "chat"
    raise notify.PermanentNotifyError("a decision card goes to a chat_id, or to a team_id and a channel_id")


def _where(target: Mapping[str, Any]) -> dict[str, str]:
    if target_kind(target) == "channel":
        return {"team_id": str(target["team_id"]), "channel_id": str(target["channel_id"])}
    return {"chat_id": str(target["chat_id"])}


def message_id_of(answer: str) -> str | None:
    """The message id a send or edit tool answered, else ``None``."""
    found = _MESSAGE_ID.search(answer or "")
    return found.group(1) if found else None


def _sender(decision_id: uuid.UUID, user_id: Any, agent_id: Any) -> SimpleNamespace:
    # What ``agent_graph_caller`` reads of an item: the sending agent and the person it acts for.
    return SimpleNamespace(id=decision_id, user_id=user_id, created_by_agent_id=agent_id, owner_agent_id=None)


async def send_link_card(
    db: AsyncSession,
    decision: Decision,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    integration: str,
    recipient_id: uuid.UUID,
    target: Mapping[str, Any],
    caller: CallerFactory | None = None,
) -> str:
    """Post the link card of *decision* for *recipient_id* to *target*, as *agent_id* acting for *user_id*.

    Returns the message id; records a ``notified`` event naming the message so
    the answer can update it. A refused send raises :class:`NotifyError` and
    records nothing.
    """
    kind = target_kind(target)
    card = answer_links.link_card(decision, recipient_id, _SURFACE[kind])
    question = str((decision.ask or {}).get("question") or "A decision is waiting for you")
    arguments: dict[str, Any] = {**_where(target), "message": question[:300], "card": card}
    async with (caller or notify.agent_graph_caller)(_sender(decision.id, user_id, agent_id), integration) as call:
        sent, answer = await call(_SEND[kind], arguments)
    message_id = message_id_of(answer) if sent else None
    if not message_id:
        raise notify.NotifyError((answer or "the decision card was not sent")[:500])
    db.add(
        DecisionEvent(
            decision_id=decision.id,
            event="notified",
            surface=_SURFACE[kind],
            detail={
                "via": VIA,
                "agent_id": str(agent_id),
                "user_id": str(user_id),
                "integration": integration,
                "recipient_id": str(recipient_id),
                **_where(target),
                "message_id": message_id,
            },
        )
    )
    await db.commit()
    return message_id


async def _sent_cards(db: AsyncSession, decision_id: uuid.UUID) -> list[dict[str, Any]]:
    result = await db.execute(
        select(DecisionEvent.detail)
        .where(DecisionEvent.decision_id == decision_id, DecisionEvent.event == "notified")
        .order_by(DecisionEvent.created_at)
    )
    rows = result.scalars().all()
    return [dict(d) for d in rows if isinstance(d, dict) and d.get("via") == VIA and d.get("message_id")]


async def has_sent_cards(db: AsyncSession, decision_id: uuid.UUID) -> bool:
    """Whether a link card was sent for *decision_id* through a Microsoft account."""
    return bool(await _sent_cards(db, decision_id))


async def update_sent_cards(
    db: AsyncSession,
    decision_id: uuid.UUID,
    *,
    answered_by_name: str | None = None,
    caller: CallerFactory | None = None,
) -> dict[str, int]:
    """Replace every link card sent for a decided *decision_id* with its decided card.

    Each message is edited as its sender. A refused edit is logged and
    counted, never raised: the links it keeps open the decided page
    (I.4 fallback). Returns ``{"updated": n, "refused": m}``.
    """
    counts = {"updated": 0, "refused": 0}
    decision = await db.get(Decision, decision_id)
    if decision is None or decision.status == "open":
        return counts
    card = answer_links.decided_card(decision, answered_by_name)
    for sent in await _sent_cards(db, decision_id):
        kind = target_kind(sent)
        arguments = {**_where(sent), "message_id": str(sent["message_id"]), "message": "", "card": card}
        try:
            factory = caller or notify.agent_graph_caller
            sender = _sender(decision_id, sent.get("user_id"), sent.get("agent_id"))
            async with factory(sender, str(sent.get("integration") or "")) as call:
                updated, answer = await call(_UPDATE[kind], arguments)
        except Exception as exc:  # a refused or failed edit leaves the link page as the decided view
            updated, answer = False, format_exception_message(exc)
        if updated:
            counts["updated"] += 1
        else:
            counts["refused"] += 1
            logger.warning(
                "Decision %s: the card %s was not updated to its decided state: %s",
                decision_id,
                sent.get("message_id"),
                (answer or "")[:300],
            )
    return counts


#: ``(decision_id) -> None``: what the answer path schedules once a decision closes.
Refresher = Callable[[uuid.UUID], Awaitable[None]]


async def refresh_after_answer(decision_id: uuid.UUID) -> None:
    """Update the sent cards of a decision just answered, on a session of its own."""
    from app.core.database import async_session
    from app.models.user import User

    try:
        async with async_session() as db:
            decision = await db.get(Decision, decision_id)
            name = None
            if decision is not None and decision.answered_by is not None:
                person = await db.get(User, decision.answered_by)
                name = (getattr(person, "full_name", None) or getattr(person, "email", None)) if person else None
            await update_sent_cards(db, decision_id, answered_by_name=name)
    except Exception:
        logger.exception("Decision %s: updating its sent cards failed", decision_id)
