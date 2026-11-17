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

"""Every message an agent sent through an agent account on its own.

A rule reply is logged by the account's pipeline: the message it answers is a
row of ``agent_account_decisions`` linked to the reply. A message the agent
sends through the same Microsoft identity from anywhere else - a chat run, an
assignment run, a tool call - answers no message the account heard. It is
logged here at send time so the account's owner sees everything the AI sent in
their name:

* one ``agent_account_replies`` row, ``kind = 'proactive'``: the text sent,
  the recipients and what produced it (``origin``), ``sent`` or ``failed``;
* one ``agent_account_decisions`` row, ``kind = 'proactive'``, linked to it:
  the entry of the activity log and of ``list_agent_account_decisions``.

The accounts are those whose integration the send went through. The log never
decides whether a send succeeds: a write that fails is a server-side warning.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import async_session
from app.models.agent_account import AgentAccount, AgentAccountDecision, AgentAccountReply

logger = logging.getLogger(__name__)

PROACTIVE = "proactive"
REPLY = "reply"
KINDS = (REPLY, PROACTIVE)
#: The longest text a log row keeps of the message (``activity.PREVIEW_CHARS``).
_PREVIEW_CHARS = 500
#: The keys of a run's context an origin keeps, and their longest value.
_ORIGIN_KEYS = ("source", "agent_id", "conversation_id", "run_id", "assignment_id")
_ORIGIN_CHARS = 200


def origin_of(context: dict[str, Any] | None) -> dict[str, str]:
    """What produced a send, from the tool context of the run (``integration_tools``)."""
    origin: dict[str, str] = {}
    for key, value in (context or {}).items():
        if key == "assignment" and value is not None:
            key, value = "assignment_id", getattr(value, "id", None) or (
                value.get("id") if isinstance(value, dict) else None
            )
        if key in _ORIGIN_KEYS and value not in (None, ""):
            origin[key] = str(value)[:_ORIGIN_CHARS]
    return origin


def _cut(value: str | None, size: int) -> str | None:
    return value[:size] if value else None


async def record_proactive_send(
    integration_id: str | None,
    *,
    provider: str,
    tool: str,
    conversation_id: str | None,
    text: str,
    conversation_type: str | None = None,
    conversation_name: str | None = None,
    message_id: str | None = None,
    recipients: list[str] | None = None,
    error: str | None = None,
    origin: dict[str, str] | None = None,
    thread_route: dict[str, str] | None = None,
) -> int:
    """Log a send through *integration_id* in the activity log of the enabled accounts of *provider* on it.

    *provider* is the account provider (``microsoft_teams``,
    ``microsoft_outlook``); *error* marks the send failed. A conversation type
    left out is the one the account's log already knows for the conversation,
    else ``direct``. *thread_route* names the channel thread a channel send
    posted in (``kind``, ``team_id``, ``channel_id``, ``root_id``): a sent
    message makes it active and followed for each account, as a rule reply
    does (:func:`follow_sent_thread`). Returns the entries written; never raises.
    """
    if not integration_id:
        return 0
    try:
        key = uuid.UUID(str(integration_id))
    except ValueError:
        return 0
    try:
        async with async_session() as db:
            accounts = (
                (
                    await db.execute(
                        # Enabled accounts only: a disabled account on the same
                        # integration (possibly another owner's) never reads the send.
                        select(AgentAccount).where(
                            AgentAccount.integration_id == key,
                            AgentAccount.provider == provider,
                            AgentAccount.enabled.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            written = 0
            for account in accounts:
                await _write(
                    db,
                    account,
                    tool=tool,
                    conversation_id=conversation_id or f"{tool}:{uuid.uuid4().hex}",
                    conversation_type=conversation_type,
                    conversation_name=conversation_name,
                    message_id=message_id or uuid.uuid4().hex,
                    text=text or "",
                    recipients=[str(r) for r in recipients or [] if r][:50],
                    error=error,
                    origin=origin or {},
                )
                written += 1
            await db.commit()
            if thread_route and not error:
                for account in accounts:
                    await follow_sent_thread(account, thread_route)
            return written
    except Exception:
        logger.warning(
            "Could not log a message sent through integration %s (%s) in its agent accounts' activity log",
            integration_id,
            tool,
            exc_info=True,
        )
        return 0


async def follow_sent_thread(account: AgentAccount, route: dict[str, str]) -> bool:
    """Make the channel thread a tool send posted in active and followed, as a rule reply does.

    A rule waiting for a mention takes the thread's follow-ups once the account
    wrote in it (``continue_threads``). A channel heard for mentions alone never
    notifies a follow-up without one, so the thread is read for them
    (``listening.follow_threads``) unless the channel is heard in full.
    Returns whether the thread is read for its follow-ups.
    """
    from app.services.agent_accounts import state
    from app.services.agent_accounts.rules import (
        AccountSettings,
        follows_channel_threads,
        hears_channel_in_full,
        listening_plan,
    )

    channel_id, root_id = route.get("channel_id"), route.get("root_id")
    if route.get("kind") != "channel" or not channel_id or not root_id or not route.get("team_id"):
        return False
    account_id, thread_id = str(account.id), f"{channel_id}:{root_id}"
    try:
        await state.mark_thread_active(account_id, thread_id)
        settings_ = AccountSettings.of(account)
        if hears_channel_in_full(listening_plan(settings_), channel_id) or not follows_channel_threads(
            settings_, channel_id
        ):
            return False
        await state.follow_thread(account_id, channel_id, thread_id, dict(route))
        return True
    except Exception:
        logger.warning("Could not follow the channel thread agent account %s posted in", account_id, exc_info=True)
        return False


async def _write(
    db,
    account: AgentAccount,
    *,
    tool: str,
    conversation_id: str,
    conversation_type: str | None,
    conversation_name: str | None,
    message_id: str,
    text: str,
    recipients: list[str],
    error: str | None,
    origin: dict[str, str],
) -> None:
    if conversation_type is None or conversation_name is None:
        known = (
            await db.execute(
                select(AgentAccountDecision.conversation_type, AgentAccountDecision.conversation_name)
                .where(
                    AgentAccountDecision.account_id == account.id,
                    AgentAccountDecision.conversation_id == conversation_id[:500],
                )
                .order_by(AgentAccountDecision.created_at.desc())
                .limit(1)
            )
        ).first()
        if known is not None:
            conversation_type = conversation_type or known[0]
            conversation_name = conversation_name or known[1]
    conversation_type = (conversation_type or "direct")[:20]
    status = "failed" if error else "sent"
    now = datetime.now(UTC)
    agent_id = origin.get("agent_id")
    reply = AgentAccountReply(
        id=uuid.uuid4(),
        account_id=account.id,
        user_id=account.user_id,
        agent_id=_uuid(agent_id) or account.agent_id,
        status=status,
        kind=PROACTIVE,
        origin={"tool": tool, **origin, "recipients": recipients},
        conversation_type=conversation_type,
        conversation_id=conversation_id[:500],
        conversation_name=_cut(conversation_name, 500),
        thread_id=conversation_id[:500],
        message_id=message_id[:255],
        sender_name=_cut(account.account_display_name or account.name, 255),
        inbound_text="",
        draft_text=text,
        sent_text=None if error else text,
        sent_message_id=None if error else message_id[:255],
        ai_label=False,
        error=_cut(error, 2000),
        created_at=now,
    )
    db.add(reply)
    await db.flush()
    values = {
        "id": uuid.uuid4(),
        "account_id": account.id,
        "outcome": status,
        "reason": None,
        "respond": True,
        "waited": False,
        "kind": PROACTIVE,
        "conversation_type": conversation_type,
        "conversation_id": conversation_id[:500],
        "conversation_name": _cut(conversation_name, 500),
        "message_id": message_id[:255],
        "sender_id": None,
        "sender_name": _cut(account.account_display_name or account.name, 255),
        "preview": text[:_PREVIEW_CHARS],
        "mentions_account": False,
        "addressed": False,
        "thread_continuation": False,
        "reply_id": reply.id,
        "message_at": now,
        "created_at": now,
        "updated_at": now,
    }
    insert = pg_insert(AgentAccountDecision).values(**values)
    # The listener may have heard the account's own message first: that row
    # becomes the send's entry.
    await db.execute(
        insert.on_conflict_do_update(
            constraint="uq_agent_account_decisions_message",
            set_={
                "outcome": insert.excluded.outcome,
                "reason": None,
                "respond": True,
                "kind": PROACTIVE,
                "preview": insert.excluded.preview,
                "reply_id": insert.excluded.reply_id,
                "updated_at": insert.excluded.updated_at,
            },
        )
    )


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None
