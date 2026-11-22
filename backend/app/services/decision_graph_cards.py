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

An email (``send_microsoft_email``, notify channel ``email``) carries the same
links as plain text (:func:`decision_answer_links.link_text`, surface
``email``). A sent email cannot be edited: its links open the decided page
once the decision is closed.
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
_SEND = {
    "chat": "send_microsoft_chat_message",
    "channel": "send_microsoft_channel_message",
    "email": "send_microsoft_email",
}
_UPDATE = {"chat": "update_microsoft_chat_message", "channel": "update_microsoft_channel_message"}
#: The surface a link minted for each kind of target records on its answer.
_SURFACE = {"chat": "teams_chat", "channel": "teams_channel", "email": "email"}
_MESSAGE_ID = re.compile(r"Message ID: ([^\s.]+)")
#: The ``notified`` events of this module carry this ``via``.
VIA = "graph_link_card"

#: ``(sender, integration) -> async context of (tool, arguments) -> (sent, answer)``; tests replace it.
CallerFactory = Callable[[Any, str], Any]


def target_kind(target: Mapping[str, Any]) -> Literal["chat", "channel", "email"]:
    """``channel`` for ``{team_id, channel_id}``, ``chat`` for ``{chat_id}``, ``email`` for ``{email}``."""
    if target.get("team_id") and target.get("channel_id"):
        return "channel"
    if target.get("chat_id"):
        return "chat"
    if target.get("email"):
        return "email"
    raise notify.PermanentNotifyError(
        "a decision card goes to a chat_id, to a team_id and a channel_id, or to an email address"
    )


def _where(target: Mapping[str, Any]) -> dict[str, str]:
    kind = target_kind(target)
    if kind == "channel":
        return {"team_id": str(target["team_id"]), "channel_id": str(target["channel_id"])}
    if kind == "email":
        return {"email": str(target["email"])}
    return {"chat_id": str(target["chat_id"])}


def _send_arguments(
    decision: Decision,
    recipient_id: uuid.UUID,
    target: Mapping[str, Any],
    language: str | None = None,
    kicker: str | None = None,
) -> dict[str, Any]:
    """The send tool's arguments: the link card for Teams, the link text for an email."""
    kind = target_kind(target)
    question = str((decision.ask or {}).get("question") or "A decision is waiting for you")
    if kind == "email":
        return {
            "to": str(target["email"]),
            "subject": question[:200],
            "body": answer_links.link_text(decision, recipient_id, _SURFACE[kind], language),
            "content_type": "text",
        }
    card = answer_links.link_card(decision, recipient_id, _SURFACE[kind], language, kicker)
    return {**_where(target), "message": question[:300], "card": card}


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
    language: str | None = None,
    kicker: str | None = None,
) -> str:
    """Post the link card of *decision* for *recipient_id* to *target*, as *agent_id* acting for *user_id*.

    Returns the message id (``""`` for an email, which has none to edit);
    records a ``notified`` event naming the message so the answer can update
    it. A refused send raises :class:`NotifyError` and records nothing.
    *language* writes the card's words, *kicker* is the line above the question.
    """
    kind = target_kind(target)
    arguments = _send_arguments(decision, recipient_id, target, language, kicker)
    async with (caller or notify.agent_graph_caller)(_sender(decision.id, user_id, agent_id), integration) as call:
        sent, answer = await call(_SEND[kind], arguments)
    message_id = (message_id_of(answer) if kind != "email" else "") if sent else None
    if message_id is None or (kind != "email" and not message_id):
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
                **({"message_id": message_id} if message_id else {"sent": True}),
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


async def sent_card_to(db: AsyncSession, decision_id: uuid.UUID, target: Mapping[str, Any]) -> str | None:
    """The message id of the link card already sent for *decision_id* to *target*, else ``None``."""
    where = _where(target)
    for sent in await _sent_cards(db, decision_id):
        if all(str(sent.get(k) or "") == v for k, v in where.items()):
            return str(sent["message_id"])
    return None


def missing_decision_field(item: Any) -> str | None:
    """What keeps *item* from carrying a decision card, named for the caller; ``None`` when nothing. Pure."""
    from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES

    if getattr(item, "status", None) in WORK_ITEM_TERMINAL_STATUSES:
        return f"status: the work item is {item.status}, so it waits for nobody"
    if not getattr(item, "needs_human", None):
        return "needs_human: the work item is not parked for a person"
    ask = getattr(item, "ask", None)
    if not isinstance(ask, dict) or not str(ask.get("question") or "").strip():
        return "ask.question: the work item's ask has no question"
    options = ask.get("options")
    if not isinstance(options, list) or len([o for o in options if isinstance(o, dict) and o.get("key")]) < 2:
        return "ask.options: the work item's ask needs at least 2 options with a key"
    return None


async def open_work_item_decision(db: AsyncSession, item_id: uuid.UUID) -> Decision:
    """The open decision of a waiting work item, opened from the item when its row is missing (#1244).

    The ``before_flush`` hook opens the row on a ``needs_human`` transition
    written through the ORM; an item parked another way (a bulk update, a
    row cancelled by a later write while the item still waits) has none. Any
    non-terminal item with ``needs_human`` and a complete ask gets its row
    here, with the hook's own rule (``work_item_opening``), whoever parked it
    and whatever its status. Raises :class:`PermanentNotifyError` naming the
    missing field otherwise.
    """
    from datetime import UTC, datetime

    from sqlalchemy.exc import IntegrityError

    from app.models.decision_hooks import work_item_opening
    from app.models.work_item import WorkItem
    from app.services.decision_card_delivery import open_decision

    row = await db.get(WorkItem, item_id)
    if row is None:
        raise notify.PermanentNotifyError("work_item: not found")
    missing = missing_decision_field(row)
    if missing is not None:
        raise notify.PermanentNotifyError(f"no decision card can be sent: {missing}")
    kind, ask, recipient, deadline = work_item_opening(row)
    now = datetime.now(UTC)
    decision = Decision(
        id=uuid.uuid4(),
        source_type="work_item",
        source_id=row.id,
        kind=kind,
        ask=ask,
        recipient=recipient,
        status="open",
        deadline=deadline,
        created_at=now,
        updated_at=now,
    )
    db.add(decision)
    db.add(DecisionEvent(decision_id=decision.id, event="asked", detail={"kind": kind, "opened_by": "card"}))
    try:
        await db.commit()
    except IntegrityError:
        # Opened meanwhile by another writer (the open-source unique index): use that one.
        await db.rollback()
        existing = await open_decision(db, "work_item", row.id)
        if existing is None:
            raise
        return existing
    logger.info("Opened the missing decision %s of work item %s for its card", decision.id, row.id)
    return decision


async def send_agent_card(
    db: AsyncSession,
    item: Any,
    *,
    agent_id: uuid.UUID,
    agent_name: str,
    user_id: uuid.UUID,
    integration: str,
    target: Mapping[str, Any],
    caller: CallerFactory | None = None,
) -> tuple[str, bool]:
    """Post the decision card of a waiting work item to *target*, as the agent of the current run.

    The agent DMs the decision itself (``send_decision_card``): no
    ``payload.notify`` is needed, so the item's end sends no text. The card is
    the link card in the recipient's language, its kicker naming the agent; the
    ``notified`` event it records lets :func:`update_sent_cards` turn it into
    the decided card whichever channel closes the decision. A test item may
    only reach a test recipient or the account's own chat. Returns
    ``(message_id, already_sent)``; raises :class:`NotifyError` with the reason
    when nothing can be sent.
    """
    from app.services.channel_notices import notice, user_notice_language
    from app.services.decision_card_delivery import open_decision

    kind = target_kind(target)
    if kind == "email":
        raise notify.PermanentNotifyError("a decision card goes to a Teams chat_id, or a team_id and a channel_id")
    if notify.is_test_item(getattr(item, "correlation_key", None), getattr(item, "title", None)):
        key = (
            (f"{target['team_id']}/{target['channel_id']}" if kind == "channel" else str(target["chat_id"]))
            .strip()
            .lower()
        )
        if key != notify.SELF_CHAT_ID and key not in notify._test_recipients():
            raise notify.PermanentNotifyError(
                "a test work item may only send its card to a target of WORK_ITEM_NOTIFY_TEST_RECIPIENTS "
                f"or the account's own chat (chat_id {notify.SELF_CHAT_ID})"
            )
    decision = await open_decision(db, "work_item", item.id)
    if decision is None:
        decision = await open_work_item_decision(db, item.id)
    recipient = recipient_user_id(decision.recipient)
    if recipient is None:
        raise notify.PermanentNotifyError(
            f"the decision waits for {decision.recipient}, not one person: its answer links are minted for one"
        )
    existing = await sent_card_to(db, decision.id, target)
    if existing is not None:
        return existing, True
    language = await user_notice_language(db, recipient)
    kicker = notice("card_from_by", language, source=notice("card_source_work_item", language), agent=agent_name)
    message_id = await send_link_card(
        db,
        decision,
        agent_id=agent_id,
        user_id=user_id,
        integration=integration,
        recipient_id=recipient,
        target=target,
        caller=caller,
        language=language,
        kicker=kicker,
    )
    return message_id, False


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
    from app.services.channel_notices import user_notice_language

    # The kept object buttons are worded in the reader's language, as the sent card was (#1177).
    language = await user_notice_language(db, recipient_user_id(decision.recipient))
    card = answer_links.decided_card(decision, answered_by_name, language)
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


#: Sends tried for one decision card before the scheduler stops trying.
MAX_SEND_ATTEMPTS = 3
#: A decision opened longer ago than this gets no card from the tick.
_SEND_WINDOW_DAYS = 2
_SEND_BATCH = 20


def card_target(item: Any) -> dict[str, str] | None:
    """Where a waiting work item's card goes: its notify ask's chat, channel or email, else ``None``.

    A Teams ask naming a person by address (``user``) has no conversation
    to post a card in yet: it keeps the text notification at the item's end.
    An ``email`` ask gets the answer links by email.
    """
    spec = notify.notify_spec(getattr(item, "payload", None))
    if not spec or not spec.get("integration"):
        return None
    if spec.get("channel") == "email" and spec.get("user"):
        return {"email": str(spec["user"])}
    if spec.get("team_id") and spec.get("channel_id"):
        return {"team_id": str(spec["team_id"]), "channel_id": str(spec["channel_id"])}
    if spec.get("chat_id"):
        return {"chat_id": str(spec["chat_id"])}
    return None


def recipient_user_id(recipient: str | None) -> uuid.UUID | None:
    """The person a link is minted for: ``user:<uuid>`` only (a group or ``company`` has no single reader)."""
    if not recipient or not recipient.startswith("user:"):
        return None
    try:
        return uuid.UUID(recipient.removeprefix("user:"))
    except ValueError:
        return None


async def _send_attempts(db: AsyncSession, decision_id: uuid.UUID) -> tuple[bool, int]:
    """Whether *decision_id* needs no more send (sent, or refused for good), and how many sends failed."""
    result = await db.execute(
        select(DecisionEvent.detail).where(DecisionEvent.decision_id == decision_id, DecisionEvent.event == "notified")
    )
    details = [d for d in result.scalars().all() if isinstance(d, dict) and d.get("via") == VIA]
    finished = any(d.get("message_id") or d.get("sent") or d.get("permanent") for d in details)
    return finished, sum(1 for d in details if d.get("error"))


async def deliver_work_item_cards(*, limit: int = _SEND_BATCH, caller: CallerFactory | None = None) -> int:
    """Post the link card of every open work item decision whose item names a Teams chat, channel or email.

    Called by the scheduler tick. The item's ``payload.notify`` names the
    Microsoft integration and the chat or channel; the agent that created the
    item sends, as for its end notification. A failed send is recorded as a
    ``notified`` event with its error and tried again on the next ticks, at
    most :data:`MAX_SEND_ATTEMPTS` times. Returns the number of cards sent.
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func

    from app.core.database import async_session
    from app.models.work_item import WorkItem

    since = datetime.now(UTC) - timedelta(days=_SEND_WINDOW_DAYS)
    sent = 0
    async with async_session() as db:
        rows = (
            await db.execute(
                select(Decision, WorkItem)
                .join(WorkItem, WorkItem.id == Decision.source_id)
                .where(
                    Decision.source_type == "work_item",
                    Decision.status == "open",
                    Decision.created_at >= since,
                    func.json_typeof(WorkItem.payload[notify.NOTIFY_KEY]) == "object",
                )
                .order_by(Decision.created_at.asc())
                .limit(limit)
            )
        ).all()
        for decision, item in rows:
            if (notify.notify_spec(item.payload) or {}).get("channel") == notify.GOOGLE_CHAT:
                # The Chat app's card, else the link message as a user (#842, S11).
                from app.services.google_chat_decision_cards import deliver_work_item_card

                sent += int(await deliver_work_item_card(db, decision, item, caller=caller))
                continue
            target = card_target(item)
            recipient = recipient_user_id(decision.recipient)
            agent_id = item.created_by_agent_id or item.owner_agent_id
            if target is None or recipient is None or agent_id is None or item.user_id is None:
                continue
            done, failures = await _send_attempts(db, decision.id)
            if done or failures >= MAX_SEND_ATTEMPTS:
                continue
            spec = notify.notify_spec(item.payload) or {}
            try:
                await send_link_card(
                    db,
                    decision,
                    agent_id=agent_id,
                    user_id=item.user_id,
                    integration=str(spec["integration"]),
                    recipient_id=recipient,
                    target=target,
                    caller=caller,
                )
                sent += 1
            except Exception as exc:  # recorded and retried on the next tick, bounded
                error = format_exception_message(exc)[:500]
                permanent = isinstance(exc, notify.PermanentNotifyError)
                db.add(
                    DecisionEvent(
                        decision_id=decision.id,
                        event="notified",
                        surface=_SURFACE[target_kind(target)],
                        detail={"via": VIA, "error": error, **({"permanent": True} if permanent else {})},
                    )
                )
                await db.commit()
                logger.warning("Decision %s: the card was not sent: %s", decision.id, error)
    return sent


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
            if decision is not None:
                # The cards the Google Chat app posted (#842, S11), edited as the app.
                from app.services.google_chat_decision_cards import update_sent_cards as update_chat_cards

                await update_chat_cards(db, decision)
    except Exception:
        logger.exception("Decision %s: updating its sent cards failed", decision_id)
