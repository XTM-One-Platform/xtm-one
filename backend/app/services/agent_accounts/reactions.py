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

"""Reactions on an agent account's answers, read as ratings of them.

Two ways a reaction reaches the platform:

* **push** - a subscription that hears ``updated`` notifies a change to a
  message (``provider.updated_message``). Most changes are not to an answer of
  the account: the message is looked up among its sent answers BEFORE anything
  is read from the provider, by the notification endpoint itself with one
  indexed query per delivery (``listening.accept_notifications``), so the noise
  costs no task. The notifications of one answer that arrive within
  ``COALESCE_WAIT_SECONDS`` of the first give one read;
* **periodic check** - every posted answer is read on a schedule until the
  reaction window closes: one no subscription hears (a channel listened to for
  mentions only, an account that polls) at a pace that slows down as the
  answer ages (``check_interval``), one push hears every
  ``PUSHED_CHECK_INTERVAL``, since push may stop hearing it before the window
  closes (a subscription removed, or recreated for mentions only).

Switching the setting off, or the account, stops the checks and keeps the
ratings already stored; switching either back on, or changing which emojis
count, checks every answer still in its window again at once
(``resume_checks``), which brings its ratings in line with the setting.

Either way the answer's FULL current reactions are read back with the
account's token and handed to ``reaction_feedback``, which owns what they
mean (the account's setting, one rating per rater, retractions).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from app.core.database import async_session
from app.models.agent_account import AgentAccount, AgentAccountReply, AgentAccountSubscription
from app.services.agent_accounts import state
from app.services.agent_accounts.pipeline import AccountUnavailableError, open_session
from app.services.agent_accounts.providers import (
    AccountProvider,
    ProviderError,
    UpdatedMessage,
    get_provider,
)
from app.services.reaction_feedback import (
    REACTION_WINDOW,
    answer_target,
    apply_reactions,
    effective_config,
    ratings_by_rater,
    within_window,
)

logger = logging.getLogger(__name__)

#: How long a notified change waits for the reactions that follow it: the first
#: notification claims the read (``state.claim_reaction_refresh``), waits this
#: long, releases the claim and reads, so the ones that arrived meanwhile are seen.
COALESCE_WAIT_SECONDS = 3.0
#: How often an answer push hears is still read: push may stop hearing it.
PUSHED_CHECK_INTERVAL = timedelta(hours=6)
#: Answers one maintenance pass checks.
CHECKS_PER_PASS = 50
#: The first periodic check after an answer is posted.
FIRST_CHECK = timedelta(minutes=5)


def source_of(provider: AccountProvider) -> str:
    """The ``source`` of a rating given by a reaction on an agent account's answer."""
    return f"{provider.platform}_agent_account"


def check_interval(age: timedelta) -> timedelta:
    """How long until an answer of *age* is checked again: reactions come early."""
    if age < timedelta(hours=1):
        return timedelta(minutes=5)
    if age < timedelta(days=1):
        return timedelta(minutes=30)
    return timedelta(hours=6)


def posted_at(reply: AgentAccountReply) -> datetime | None:
    """When the answer was posted: approved later than drafted, or sent right away."""
    return reply.decided_at or reply.created_at


def reactions_wanted(account: AgentAccount, provider: AccountProvider | None) -> bool:
    """Whether the account's reactions are read at all."""
    return (
        provider is not None
        and provider.capabilities.reactions
        and effective_config(account.reaction_feedback).enabled
    )


async def schedule_first_check(account: AgentAccount, reply: AgentAccountReply) -> None:
    """Put a just-posted answer on the periodic schedule, whether push hears it or not."""
    if reply.id is None or not reactions_wanted(account, get_provider(account.provider)):
        return
    await state.schedule_reaction_check(str(reply.id), time.time() + FIRST_CHECK.total_seconds())


async def resume_checks(db, account: AgentAccount) -> int:
    """Check every answer of *account* still in its window again, now; how many.

    Called when its reactions start to count again or count otherwise: the
    setting switched on or changed, the account enabled again. A check drops
    an answer while either is off (the ratings it gave stay), so a reaction
    put meanwhile, or a rating given by an emoji the setting no longer maps,
    is brought in line by the next read of the answer's full reactions.
    """
    if not account.enabled or not reactions_wanted(account, get_provider(account.provider)):
        return 0
    cutoff = datetime.now(UTC) - REACTION_WINDOW
    ids = (
        await db.execute(
            select(AgentAccountReply.id).where(
                AgentAccountReply.account_id == account.id,
                AgentAccountReply.status == "sent",
                AgentAccountReply.sent_message_id.is_not(None),
                # Posted (``posted_at``) within the window; approved later than drafted.
                or_(AgentAccountReply.decided_at >= cutoff, AgentAccountReply.created_at >= cutoff),
            )
        )
    ).scalars().all()
    now = time.time()
    await state.schedule_reaction_checks({str(reply_id): now for reply_id in ids})
    return len(ids)


# ---------------------------------------------------------------------------
# Reading one answer
# ---------------------------------------------------------------------------


async def _subscriptions(db, account_id: uuid.UUID) -> list[tuple[str, str]]:
    rows = await db.execute(
        select(AgentAccountSubscription.resource, AgentAccountSubscription.change_type).where(
            AgentAccountSubscription.account_id == account_id
        )
    )
    return [(resource, change_type) for resource, change_type in rows.all()]


async def refresh_reply(db, account: AgentAccount, reply: AgentAccountReply) -> bool:
    """Read the answer's reactions and store the ratings they give. False when it cannot be read."""
    provider = get_provider(account.provider)
    if not account.enabled or not reactions_wanted(account, provider) or reply.status != "sent":
        return False
    if not reply.sent_message_id or not within_window(posted_at(reply)):
        return False
    agent_id = reply.agent_id or account.agent_id
    if agent_id is None:
        return False
    try:
        session = await open_session(db, account, provider)
        reactions = await provider.read_reactions(session, reply.route or {}, reply.sent_message_id)
    except (AccountUnavailableError, ProviderError) as exc:
        logger.info("Agent account %s could not read the reactions of an answer: %s", account.id, exc)
        return False
    if reactions is None:
        return False
    # An AI user's account is the agent: its own reaction rates nothing. A
    # delegate's account is its owner, whose reaction is a rating like anyone's.
    own = [account.account_external_id] if account.mode == "ai_user" and account.account_external_id else []
    ratings = ratings_by_rater(
        effective_config(account.reaction_feedback),
        reactions,
        platform=provider.key,
        exclude_user_ids=own,
    )
    await apply_reactions(db, answer_target(reply.id, agent_id), ratings, source=source_of(provider))
    return True


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


async def on_updated_message(account_id: str, updated: UpdatedMessage) -> None:
    """A trusted notification says a message changed: if it is one of the account's
    answers, read its reactions once the burst it belongs to has arrived. Never raises."""
    try:
        async with async_session() as db:
            account = await db.get(AgentAccount, uuid.UUID(account_id))
            if account is None or not account.enabled or not reactions_wanted(account, get_provider(account.provider)):
                return
            found = await _find_reply(db, account.id, updated)
        if found is None:
            return
        if not await state.claim_reaction_refresh(account_id, updated.conversation_id, updated.message_id):
            return
        await asyncio.sleep(COALESCE_WAIT_SECONDS)
        await state.release_reaction_refresh(account_id, updated.conversation_id, updated.message_id)
        async with async_session() as db:
            account = await db.get(AgentAccount, uuid.UUID(account_id))
            reply = await db.get(AgentAccountReply, found)
            if account is not None and reply is not None:
                await refresh_reply(db, account, reply)
    except Exception:
        logger.exception("Agent account %s failed on a changed message", account_id)


async def _find_reply(db, account_id: uuid.UUID, updated: UpdatedMessage) -> uuid.UUID | None:
    """The account's sent answer *updated* names (``ix_agent_account_replies_sent_message``)."""
    return await db.scalar(
        select(AgentAccountReply.id).where(
            AgentAccountReply.account_id == account_id,
            AgentAccountReply.sent_message_id == updated.message_id,
            AgentAccountReply.conversation_id == updated.conversation_id,
            AgentAccountReply.status == "sent",
        ).limit(1)
    )


# ---------------------------------------------------------------------------
# Periodic checks (the scheduler's maintenance step)
# ---------------------------------------------------------------------------


async def run_due_checks(limit: int = CHECKS_PER_PASS) -> int:
    """Check the answers whose check is due; how many were read."""
    read = 0
    for reply_id in await state.due_reaction_checks(limit):
        try:
            if await _check(reply_id):
                read += 1
        except Exception:
            logger.exception("Agent account reaction check of %s failed", reply_id)
    return read


async def _check(reply_id: str) -> bool:
    try:
        parsed = uuid.UUID(reply_id)
    except ValueError:
        return False
    async with async_session() as db:
        reply = await db.get(AgentAccountReply, parsed)
        account = await db.get(AgentAccount, reply.account_id) if reply is not None else None
        if reply is None or account is None or reply.status != "sent":
            return False
        provider = get_provider(account.provider)
        if provider is None or not within_window(posted_at(reply)):
            return False
        if not account.enabled or not reactions_wanted(account, provider):
            # Off: dropped, the ratings stored so far kept. ``resume_checks``
            # puts the answer back when the setting or the account is on again.
            return False
        age = datetime.now(UTC) - (posted_at(reply) or datetime.now(UTC))
        interval = check_interval(age)
        if provider.reactions_pushed(reply.route or {}, await _subscriptions(db, account.id)):
            # Heard through push, which may stop hearing it before the window
            # closes: still read, slowly.
            interval = max(interval, PUSHED_CHECK_INTERVAL)
        read = await refresh_reply(db, account, reply)
        await state.schedule_reaction_check(reply_id, time.time() + interval.total_seconds())
        return read
