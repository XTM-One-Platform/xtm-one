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

"""Integration event poller – checks for new events from integrations.

Handles `integration_event` assignment triggers by polling external APIs
for new data (emails, messages, etc.) and triggering matching assignments.

A Microsoft assignment whose Graph subscription is active
(``services/graph_event_subscriptions.py``, marked by its ``graph_sub_wf:``
key) is skipped: its events arrive on
``POST /api/v1/integrations/notifications/microsoft``. When the subscription
cannot be created or lapses, the key goes and this poller reads on from the
last event notified (``advance_event_cursor``).

Every event goes through the trigger's filter
(``services/integration_event_filters.py``) before a run is created.

Deduplication strategy:
  - Each polled message is tracked by its unique ID in a Redis set
  - Messages already seen are never re-processed
  - The set expires after 7 days to avoid unbounded growth
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses
from typing import Any
from urllib.parse import quote

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.egress import EgressRoute, egress_client, egress_route
from app.core.redis import redis_client
from app.core.security import decrypt_value
from app.integrations.providers import docusign
from app.integrations.providers.google import GMAIL_API
from app.integrations.providers.microsoft import GRAPH_API
from app.integrations.providers.microsoft._message_type import message_type
from app.integrations.providers.odoo import events as odoo_events
from app.integrations.providers.quotapath import events as quotapath_events
from app.models.agent import Agent
from app.models.assignment import Assignment, event_read_from_legacy_key
from app.models.integration import Integration
from app.services import (
    dealhub_events,
    event_coalescing,
    jira_events,
    netsuite_events,
    opencrq_job_events,
    pennylane_events,
)
from app.services.agent_posts import (
    is_agent_mail,
    is_agent_slack_post,
    is_agent_teams_post,
)
from app.services.assignment_dispatch import CONVERSATION_KEY_FIELD
from app.services.assignment_engine import (
    batched_wakes,
    full_queue_assignment_ids,
    trigger_or_queue_assignment,
)
from app.services.assignment_pause import (
    AssignmentOwnerDeactivatedError,
    owner_not_deactivated,
)
from app.services.graph_event_subscriptions import CATCH_UP_KEY_PREFIX
from app.services.integration_credentials import agent_acting_user_id
from app.services.integration_event_filters import (
    FILTER_SIGNALS_KEY,
    event_filter_reason,
    needs_mail_headers,
    needs_mailbox,
    reason_label,
    record_filter_reasons,
    record_filtered_events,
    trigger_filter,
    without_filter_signals,
)
from app.services.integration_user_credentials import connected_for_clause, reader_clause, resolve_token_holder
from app.services.relays import integration_egress_route
from app.services.slack_conversations import conversation_key as slack_conversation_key
from app.services.slack_events import (
    assignment_slack_channels,
    claim_seen,
    release_seen,
    slack_message_key,
    slack_message_skipped,
)
from app.services.slack_events import seen_set_key as event_seen_set_key
from app.services.teams_chat_posts import (
    chat_conversation_key,
    is_agent_teams_chat_post,
    thread_conversation_key,
)
from app.services.trigger_integration_access import (
    TriggerIntegrationVisibility,
    assignment_acting_user_id,
    log_unseen_trigger_integration,
    trigger_integration_id,
)

logger = logging.getLogger(__name__)

# How often each event type should be polled (seconds)
POLL_INTERVALS = {
    "new_email": 300,  # 5 min
    "new_teams_message": 300,  # 5 min
    "new_message": 300,  # 5 min
    "new_slack_message": 300,  # 5 min
    "new_teams_reply": 300,
    "new_teams_chat_message": 300,
    "calendar_event_changed": 300,
    "email_flag_changed": 300,
    # A task a person adds is picked up within the minute when no Graph
    # subscription delivers it (a list named by name, no public URL).
    "todo_task_changed": 60,
    # DocuSign asks integrations not to poll envelope changes more often than
    # every 15 minutes.
    **dict.fromkeys(sorted(docusign.ENVELOPE_EVENTS), 900),
    # OpenCRQ job runs (opencrq_job_events): no outbound webhook to wait for.
    **dict.fromkeys(sorted(opencrq_job_events.EVENTS), opencrq_job_events.POLL_INTERVAL_SECONDS),
    # Jira issue events (jira_events): an assignment is acted on within the minute.
    **dict.fromkeys(sorted(jira_events.EVENTS), jira_events.POLL_INTERVAL_SECONDS),
    # QuotaPath payouts (providers/quotapath/events.py): no webhook in its API.
    **dict.fromkeys(sorted(quotapath_events.EVENTS), quotapath_events.POLL_INTERVAL_SECONDS),
    # Odoo records (providers/odoo/events.py): no webhook in the external API.
    **dict.fromkeys(sorted(odoo_events.EVENTS), odoo_events.POLL_INTERVAL_SECONDS),
    # Pennylane changelogs (pennylane_events): no webhook to a company token.
    **dict.fromkeys(sorted(pennylane_events.EVENTS), pennylane_events.POLL_INTERVAL_SECONDS),
    # DealHub quote status changes (dealhub_events): the fallback of its webhooks.
    **dict.fromkeys(sorted(dealhub_events.EVENTS), dealhub_events.POLL_INTERVAL_SECONDS),
    # NetSuite change events (netsuite_events): SuiteQL every 5 min, within the account's concurrency limit.
    **dict.fromkeys(sorted(netsuite_events.EVENTS), netsuite_events.POLL_INTERVAL_SECONDS),
}

_MAX_DISCOVERED_CHANNELS = 20
# The latest threads of a channel a reply poll reads, with their replies.
_TEAMS_REPLY_THREADS = 50
# The chats a chat poll reads when the trigger names none.
_MAX_DISCOVERED_CHATS = 20
_CHAT_LIST_PAGE = 50
_CALENDAR_PAGE = 25
_CALENDAR_SELECT = (
    "id,subject,start,end,location,organizer,isCancelled,createdDateTime,"
    "lastModifiedDateTime,seriesMasterId,webLink,iCalUId,attendees"
)
# An event first modified within this of its creation was created, not updated.
_CREATED_WITHIN = timedelta(seconds=60)
_FLAG_PAGE = 50
_TODO_PAGE = 50
_TODO_MAX_LISTS = 50
_TODO_SELECT = "id,title,body,status,importance,dueDateTime,createdDateTime,lastModifiedDateTime,completedDateTime"
_FLAG_STATE_TTL_SECONDS = 86400 * 30
_MAILBOX_TTL_SECONDS = 86400

# What one poll dispatches after the cursor: the oldest events, the rest left at
# the provider for the next poll. A first poll (no cursor) reads the
# latest page instead.
_MICROSOFT_MAIL_PAGE = 15
# Graph writes ``receivedDateTime`` to the second but filters on its sub-second
# value, so a second holding more mails than a page is read whole, at most this
# many mails, before the cursor moves past it.
_MICROSOFT_MAIL_SECOND_PAGE = 100
_MICROSOFT_MAIL_SECOND_MAX_PAGES = 10
_MICROSOFT_MAIL_SELECT = (
    "id,from,subject,bodyPreview,receivedDateTime,flag,conversationId,"
    "sentDateTime,toRecipients,ccRecipients,internetMessageId"
)
_CHANNEL_MESSAGE_PAGE = 20
_GMAIL_PAGE = 10
_DOCUSIGN_PAGE = 50
_OPENCRQ_PAGE = 50
# Changelog entries one Pennylane poll reads: each selected one costs a record read.
_PENNYLANE_PAGE = 50
# Pages one poll reads back to back while the provider has more; past
# them, the next tick reads on.
_MAX_PAGES_PER_POLL = 5
# Past this much of a tick, a poll reads no further page: a slow provider must
# not push the tick past the scheduler's 60 s interval. Every due assignment
# still reads its first page, and one cut short reads on next tick.
_POLL_TICK_BUDGET_SECONDS = 40.0
# When a poll last read a page, a backlog or a catch-up included: the
# ``event_poll:`` stamp is the interval gate and is written only once the poll
# caught up, so ``delivery_mode`` reads this one too and never shows a stale
# last poll while the pages keep creating runs (#1718).
POLL_READ_KEY_PREFIX = "event_poll_read:"

# Teams, Slack and Gmail cannot list the oldest first: a poll reads back to its
# cursor, at most this far, and dispatches the oldest of what it read. Past it,
# the oldest events are not fetched, which is logged.
_TEAMS_LIST_PAGE = 50  # Graph's maximum $top on channel messages
_TEAMS_MAX_LIST_PAGES = 5
_SLACK_LIST_PAGE = 200
_SLACK_MAX_LIST_PAGES = 5
_GMAIL_LIST_MAX = 100
_GMAIL_DATE_CONCURRENCY = 5
# The headers a Gmail poll reads besides From / Subject / Date when the trigger
# has a filter (``integration_event_filters``).
_GMAIL_FILTER_HEADERS = [
    "To",
    "Cc",
    "Auto-Submitted",
    "Precedence",
    "List-Id",
    "List-Unsubscribe",
    "Content-Type",
    "Return-Path",
    "X-Auto-Response-Suppress",
]

_CURSOR_TTL_SECONDS = 86400 * 30
_BACKLOG_NOTICE_TTL_SECONDS = 86400


@dataclass
class _Page[T]:
    """The events one poll dispatches, oldest first, and where the cursor goes."""

    items: list[T]
    # The item whose timestamp the cursor moves to (``None``: nothing read).
    cursor: T | None
    # Events newer than the page are left for the next poll.
    more: bool
    # The whole page shares one timestamp with the events past it: those are not fetched.
    cut: bool


def _oldest_page[T](ordered: list[T], instant: Callable[[T], Any], size: int) -> _Page[T]:
    """The *size* oldest of *ordered* (sorted oldest first) and where the cursor goes.

    Every provider reads strictly after the cursor. It stops on the page's newest
    timestamp, or on the one before it when an event past the page shares that
    timestamp: the next poll then reads the page's events at that timestamp again,
    which the seen set skips, and the ones past the page.
    """
    taken, rest = ordered[:size], ordered[size:]
    if not taken:
        return _Page([], None, False, False)
    newest = instant(taken[-1])
    if rest and instant(rest[0]) == newest:
        earlier = [item for item in taken if instant(item) < newest]
        if earlier:
            return _Page(taken, earlier[-1], True, False)
        return _Page(taken, taken[-1], True, True)
    return _Page(taken, taken[-1], bool(rest), False)


_FRACTION_PAST_MICROSECONDS = re.compile(r"(\.\d{6})\d+")


def _iso_instant(value: object) -> datetime:
    """A Graph timestamp as an aware datetime; the oldest one possible when unreadable.

    Graph writes up to seven fractional digits, one more than a datetime holds.
    """
    try:
        parsed = datetime.fromisoformat(_FRACTION_PAST_MICROSECONDS.sub(r"\1", str(value)))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _note_backlog(redis: Redis, wf: Assignment, source: str, more: bool) -> None:
    """Log once when a poll leaves new events at the provider for the next ones."""
    if isinstance(redis, _HeldCursor):
        # The poll reads the next page at once.
        redis.more = redis.more or more
    key = f"event_backlog:{wf.id}"
    try:
        if not more:
            await redis.delete(key)
        elif await redis.set(key, "1", ex=_BACKLOG_NOTICE_TTL_SECONDS, nx=True):
            logger.info(
                "Integration-event assignment %s: %s has more new events than one page "
                "holds - the poll reads them page after page, oldest first",
                wf.id,
                source,
            )
    except Exception as exc:
        logger.debug("Could not record the event backlog of assignment %s: %r", wf.id, exc)


def _log_unfetched(wf: Assignment, source: str, reason: str) -> None:
    """The one case a poll loses events: say which source, and why."""
    logger.warning(
        "Integration-event assignment %s: %s %s, so some of its new events are not fetched",
        wf.id,
        source,
        reason,
    )


def _log_page_cut(wf: Assignment, source: str, page: _Page) -> None:
    if page.cut:
        _log_unfetched(wf, source, f"has more than {len(page.items)} new events sharing one timestamp")


def _gmail_internal_date(message: dict) -> int:
    """When Gmail received *message* (epoch ms); 0 when it does not say."""
    try:
        return int(message.get("internalDate") or 0)
    except (TypeError, ValueError):
        return 0


def _int_or_zero(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _slack_ts(ts: str) -> float:
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


def _mail_received_at(message: dict) -> datetime:
    return _iso_instant(message.get("receivedDateTime"))


def _teams_created_at(message: dict) -> datetime:
    return _iso_instant(message.get("createdDateTime"))


def _teams_touched_at(message: dict) -> datetime:
    """The last change to a channel message: never before it was created."""
    return max(_teams_created_at(message), _iso_instant(message.get("lastModifiedDateTime")))


class _HeldCursor:
    """The Redis a provider poller gets, holding back its write of the cursor.

    Every provider poller moves ``event_cursor:<assignment>`` past what it fetched
    before the poller dispatches any of it. Held here, the move is only written
    once the batch was dispatched, so a worker killed in between fetches the batch
    again. Everything else goes to Redis.
    """

    def __init__(self, redis: Redis, cursor_key: str, cursor: str | None):
        self._redis = redis
        self._cursor_key = cursor_key
        self.cursor = cursor
        # The provider has more new events past the page (``_note_backlog``).
        self.more = False

    def __getattr__(self, name: str):
        return getattr(self._redis, name)

    async def get(self, key: str):
        if key == self._cursor_key:
            return self.cursor
        return await self._redis.get(key)

    async def set(self, key: str, value, *args, **kwargs):
        if key == self._cursor_key:
            self.cursor = value
            return True
        return await self._redis.set(key, value, *args, **kwargs)


async def _restore_cursor(redis: Redis, cursor_key: str, value: str | None) -> None:
    if value is None:
        await redis.delete(cursor_key)
    else:
        await redis.set(cursor_key, value, ex=_CURSOR_TTL_SECONDS)


async def give_back_polled_event(
    redis: Redis,
    *,
    msg_id: str,
    cursor_key: str,
    cursor_before: str | None,
    seen_set_key: str | None = None,
    assignment_id: object = None,
) -> None:
    """Un-consume a polled event the poll could not dispatch now.

    The event leaves the seen set and the provider cursor this poll advanced
    is put back, so the next poll fetches it - and every event after it in
    this poll, which was never marked seen - again. The events this poll did
    dispatch stay seen and are skipped then: each event runs exactly once.

    An assignment's seen set is the sorted set it shares with the Slack events
    endpoint, released through ``release_seen`` (pass ``assignment_id``); a
    flow's is a plain set (pass ``seen_set_key``).
    """
    if msg_id:
        if assignment_id is not None:
            await release_seen(redis, assignment_id, msg_id)
        elif seen_set_key is not None:
            await redis.srem(seen_set_key, msg_id)
    await _restore_cursor(redis, cursor_key, cursor_before)


async def _hand_back_batch(
    redis: Redis,
    *,
    msg_id: str,
    cursor_key: str,
    cursor_before: str | None,
    cursor_after: str | None,
    assignment_id: object,
    replay_unsafe: bool,
) -> None:
    """Give an assignment's polled event back, unless the rewind would run one twice.

    *replay_unsafe*: the batch holds an event without an id, which nothing in the
    seen set could skip on a second fetch. The cursor is left past the
    whole batch (*cursor_after*) instead, which leaves the events not dispatched yet
    unrun: logged, never duplicated.
    """
    if replay_unsafe:
        logger.warning(
            "Integration-event assignment %s: this poll holds an event without an id, so "
            "its events that cannot run now are not fetched again",
            assignment_id,
        )
        await _restore_cursor(redis, cursor_key, cursor_after)
        return
    await give_back_polled_event(
        redis,
        msg_id=msg_id,
        cursor_key=cursor_key,
        cursor_before=cursor_before,
        assignment_id=assignment_id,
    )


async def _dispatch_poll_page(
    db: AsyncSession,
    redis: Redis,
    http: httpx.AsyncClient,
    *,
    wf: Assignment,
    agent: Agent,
    provider: str,
    event_type: str,
    access_token: str,
    integration_id: object,
    route: EgressRoute | None = None,
) -> tuple[int, str]:
    """Read one page of *wf*'s new events and start or queue a run for each.

    The poll itself follows the integration's *route* (its relay group);
    the runs it starts do not inherit it.

    Returns how many events were dispatched and what the poll does next:
    ``caught_up`` (stamp the poll), ``more`` (the provider has more new events
    past the page: read the next one), ``queue_full`` (the rest was given back,
    polled again once the queue has room), ``owner_paused`` / ``refused`` (given
    back, see below) or ``seeded``.
    """
    from fastapi import HTTPException

    from app.services.agentic_gate import refusal_code

    # Shared with the Slack events endpoint, which claims a message here before
    # triggering it in real time.
    seen_set_key = event_seen_set_key(wf.id)
    # Every provider poller advances this cursor before returning. Held back
    # (``_HeldCursor``) and written once the batch was dispatched, so a restart
    # or an error in the middle fetches the rest again and the seen set skips
    # what already ran.
    cursor_key = f"event_cursor:{wf.id}"
    cursor_before = await redis.get(cursor_key)
    held_cursor = _HeldCursor(redis, cursor_key, cursor_before)
    with egress_route(route):
        events = await _poll_provider(
            provider,
            event_type,
            access_token,
            wf,
            held_cursor,
            seen_set_key,
            http,
            integration_id=str(integration_id),
        )
    cursor_after = held_cursor.cursor
    # An event without an id could not be skipped on a second fetch: such a
    # batch keeps its cursor from the start and is dispatched -
    # never twice. With no event, the cursor the provider wrote (a first
    # poll's "from now") stands.
    replay_unsafe = any(not e.get("message_id") for e in events)
    cursor_pending = cursor_after != cursor_before
    if cursor_pending and (replay_unsafe or not events):
        await _restore_cursor(redis, cursor_key, cursor_after)
        cursor_pending = False

    # A row the agent builder stored with ``event_type`` is read through
    # that key only once its trigger is switched to exact matching
    # (``read_trigger_config``): it was never polled before, so it has no cursor
    # and its first poll returns the provider's latest page. It starts from now:
    # that page is marked seen, nothing runs on it. The row counts as seeded only
    # once the provider established its cursor: a failed request returns [] and
    # writes none, and the next poll must seed rather than run the page.
    seeded_key = f"event_legacy_seeded:{wf.id}"
    if cursor_before is None and event_read_from_legacy_key(wf) and not await redis.get(seeded_key):
        if cursor_pending:
            await _restore_cursor(redis, cursor_key, cursor_after)
        for event_payload in events:
            msg_id = event_payload.get("message_id", "")
            if msg_id:
                await claim_seen(redis, wf.id, msg_id)
        if await redis.get(cursor_key):
            await redis.set(seeded_key, "1", ex=_CURSOR_TTL_SECONDS)
        logger.info(
            "Integration-event assignment %s selects its event through the "
            "legacy event_type key: its first poll marked %d existing event(s) "
            "seen without running them",
            wf.id,
            len(events),
        )
        return 0, "seeded"

    filter_config = trigger_filter(getattr(wf, "trigger_config", None))
    mailbox: set[str] | None = None
    filtered = 0
    reasons: dict[str, int] = {}
    # A dropped event is counted however the page ends.
    try:
        dispatched = 0
        for event_payload in events:
            msg_id = event_payload.get("message_id", "")
            # Atomic check-and-add to prevent duplicate triggers under concurrent
            # poll cycles
            if msg_id and not await claim_seen(redis, wf.id, msg_id):
                without_filter_signals(event_payload)
                continue

            # The trigger's filter, before any run or gate: a dropped event stays
            # seen and is only counted.
            if filter_config:
                if mailbox is None and needs_mailbox(filter_config):
                    with egress_route(route):
                        mailbox = await mailbox_addresses(
                            redis, http, provider, access_token, str(integration_id)
                        )
                reason = event_filter_reason(
                    event_type, event_payload, filter_config, mailbox=mailbox or ()
                )
                if reason is not None:
                    filtered += 1
                    label = reason_label(reason)
                    reasons[label] = reasons.get(label, 0) + 1
                    logger.debug("Integration-event assignment %s: event %s filtered out (%s)", wf.id, msg_id, reason)
                    without_filter_signals(event_payload)
                    continue
            without_filter_signals(event_payload)

            event_trigger_context = {
                "type": "integration_event",
                "provider": provider,
                "event": event_type,
                "integration_id": integration_id,
                "message_id": msg_id or None,
                "triggered_at": datetime.now(UTC).isoformat(),
            }
            # A poller that coalesces its events (``event_coalescing``): the
            # dispatcher folds this one into a queued run of the same key.
            coalesce_key = event_coalescing.payload_key(event_payload)
            if coalesce_key:
                event_trigger_context[event_coalescing.CONTEXT_KEY] = coalesce_key
            try:
                # Started in a free slot, else queued: a polled event has a
                # cursor to fall back on, so it always waits in the queue rather
                # than being refused by the overlap guard, whatever
                # INTEGRATION_EVENT_QUEUE_ENABLED says.
                result = await trigger_or_queue_assignment(
                    wf,
                    agent,
                    event_payload,
                    db=db,
                    trigger_context=event_trigger_context,
                    overflow="reject",
                )
            except HTTPException as exc:
                owner_paused = isinstance(exc, AssignmentOwnerDeactivatedError)
                if refusal_code(exc) is None and not owner_paused:
                    raise
                # Refused in the window since the poll's check, or its owner
                # deactivated since the query picked it: give the event back,
                # and the provider cursor this poll advanced, so everything it
                # returned is fetched again once it may run. The events already
                # dispatched stay in the seen set and are not dispatched twice.
                await _hand_back_batch(
                    redis,
                    msg_id=msg_id,
                    cursor_key=cursor_key,
                    cursor_before=cursor_before,
                    cursor_after=cursor_after,
                    assignment_id=wf.id,
                    replay_unsafe=replay_unsafe,
                )
                return dispatched, "owner_paused" if owner_paused else "refused"
            if result.status == "queue_full":
                # The queue is full: give the event back, and the cursor, so a later
                # poll fetches it again (and what follows it).
                await _hand_back_batch(
                    redis,
                    msg_id=msg_id,
                    cursor_key=cursor_key,
                    cursor_before=cursor_before,
                    cursor_after=cursor_after,
                    assignment_id=wf.id,
                    replay_unsafe=replay_unsafe,
                )
                return dispatched, "queue_full"
            if coalesce_key:
                # Every event the coalesced one stands for is seen now: a page
                # read again past a backed-off cursor starts nothing twice.
                for covered in event_payload.get("covers") or []:
                    covered_id = covered.get("message_id") if isinstance(covered, dict) else None
                    if covered_id and covered_id != msg_id:
                        await claim_seen(redis, wf.id, covered_id)
            dispatched += 1

        # The whole batch was dispatched: the cursor moves past it.
        if cursor_pending:
            await _restore_cursor(redis, cursor_key, cursor_after)
        if held_cursor.more and cursor_after != cursor_before:
            return dispatched, "more"
        return dispatched, "caught_up"
    finally:
        await record_filtered_events(wf.id, filtered)
        await record_filter_reasons(wf.id, reasons)


async def poll_integration_events(db: AsyncSession) -> int:
    """Poll for new integration events and trigger matching assignments.

    Called by the autonomous scheduler every 60 seconds. Tracks last-polled
    timestamps per assignment in Redis to respect poll intervals.

    Performance: uses a single shared httpx client for all external API
    calls within one tick, avoiding the overhead of creating/destroying
    TCP connections per assignment.
    """
    tick_started = time.monotonic()
    # Find all enabled integration_event assignments. One of a deactivated
    # owner (not company-managed) is not polled, so its cursor stays where it was.
    result = await db.execute(
        select(Assignment).where(
            Assignment.trigger_type == "integration_event",
            Assignment.enabled.is_(True),
            owner_not_deactivated(),
        )
    )
    assignments = result.scalars().all()
    triggered = 0
    redis = await redis_client()

    # Batch-load agents for all event assignments (avoid N+1)
    agent_ids = {wf.agent_id for wf in assignments if wf.agent_id}
    agents_map: dict = {}
    if agent_ids:
        agent_result = await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))
        agents_map = {a.id: a for a in agent_result.scalars().all()}

    from app.services.agentic_gate import agentic_refusal, group_verdict

    visibility = TriggerIntegrationVisibility()
    # Read once per tick, by the first assignment due for a poll. One
    # whose queue fills during the tick hands its events back on the spot.
    full_queue_ids: set | None = None
    async with egress_client() as http:
        for wf in assignments:
            # Asked again per assignment (a cached read), before anything is
            # polled or claimed: an edition change mid-tick then leaves the
            # provider's events unread - polled once AI is back - instead of
            # marked seen with no run behind them.
            if await agentic_refusal(db) is not None:
                break
            # The person it runs as (``_execution_identity`` with no trigger):
            # with AI off for their group, nothing is polled for it - polled,
            # its events would only be handed back on every tick - and the
            # other assignments go on.
            owner = getattr(wf, "run_as_user_id", None) or getattr(wf, "user_id", None)
            if (await group_verdict(db, owner)).refusal is not None:
                continue
            try:
                config = wf.trigger_config or {}
                provider = config.get("provider", "")
                event_type = config.get("event", "")

                if not provider or not event_type:
                    continue

                # Skip if a Graph subscription is active (real-time mode),
                # unless Graph said it missed notifications: then one poll
                # reads them back, the seen set skipping what already ran.
                sub_key = f"graph_sub_wf:{wf.id}"
                catch_up_key = f"{CATCH_UP_KEY_PREFIX}{wf.id}"
                catch_up = False
                if await redis.get(sub_key):
                    if not await redis.get(catch_up_key):
                        continue
                    catch_up = True

                # Check poll interval
                poll_key = f"event_poll:{wf.id}"
                last_poll = await redis.get(poll_key)
                interval = POLL_INTERVALS.get(event_type, 300)
                if provider == jira_events.PROVIDER:
                    # Its Jira webhook delivered within the day: the poll is a safety net.
                    from app.services import jira_webhooks

                    delivered = bool(await redis.get(jira_webhooks.delivered_key(wf.id)))
                    interval = jira_webhooks.poll_interval(interval, delivered)
                if last_poll and not catch_up:
                    elapsed = datetime.now(UTC).timestamp() - float(last_poll)
                    if elapsed < interval:
                        continue

                # Its delivery queue is full: every event this poll read would be
                # handed back. Left unpolled and unstamped, the provider keeps
                # them for the first tick that finds room.
                if full_queue_ids is None:
                    full_queue_ids = await full_queue_assignment_ids(db, [a.id for a in assignments])
                if wf.id in full_queue_ids:
                    continue

                # Get the agent (batch-loaded)
                agent = agents_map.get(wf.agent_id)
                if not agent or not agent.enabled:
                    continue

                # Get the integration — prefer explicit binding from trigger config,
                # else the agent's binding, else its template binding resolved for
                # the user the automation runs as (#1378).
                integration_id, reader_id, skip = await _poll_integration(db, wf, agent, config)
                if not integration_id:
                    await note_poll_skip(redis, wf.id, skip)
                    continue

                # OAuth tokens are personal to whoever connected them: a bound
                # integration is polled on behalf of the agent, its owner the
                # acting identity for the ownership check; a template-resolved one
                # on behalf of the user it was resolved for.
                int_result = await db.execute(
                    select(Integration).where(
                        Integration.id == integration_id,
                        connected_for_clause(reader_id),
                        reader_clause(reader_id),
                    )
                )
                integration = int_result.scalar_one_or_none()
                # The owner's own account on a per_user integration.
                holder = await resolve_token_holder(db, integration, reader_id) if integration is not None else None
                access_token = (
                    await _poll_credential(provider, integration, holder, db=db, user_id=reader_id)
                    if integration is not None and holder is not None
                    else None
                )
                if not access_token:
                    await note_poll_skip(
                        redis,
                        wf.id,
                        f"no usable credential on integration {integration_id} for its reader"
                        if integration is not None
                        else f"integration {integration_id} is not connected or not readable for its reader",
                    )
                    continue
                await note_poll_skip(redis, wf.id, None)
                # The events go to a run acting as this user: delivered even
                # when they cannot see the integration they come from, and
                # said so. No acting user (an owner-less
                # company-managed assignment): the gate above alone.
                acting_user_id = assignment_acting_user_id(wf, agent)
                if acting_user_id is not None and not await visibility.sees(
                    db, acting_user_id, integration.id
                ):
                    log_unseen_trigger_integration(
                        "assignment",
                        wf.id,
                        integration_id=integration.id,
                        acting_user_id=acting_user_id,
                    )

                route = await integration_egress_route(db, integration)

                # Hand the pooled connection back before the external poll
                # HTTP call — this loop runs every scheduler tick and the
                # reads above would otherwise pin the tick session's
                # connection across sequential Graph/Slack/Google requests.
                # ``trigger_assignment`` below commits its
                # own writes.
                from app.core.database import release_db_connection_before_loop

                await release_db_connection_before_loop(db)

                # Page after page, oldest first, while the provider has more new
                # events than a page. The poll is stamped only once it
                # caught up: a backlog is read on the next tick, never a poll
                # interval later, and so is a page past the tick's budget.
                # The reconciler is woken once per page, not once per run.
                outcome = "more"
                for page in range(_MAX_PAGES_PER_POLL):
                    if page and time.monotonic() - tick_started >= _POLL_TICK_BUDGET_SECONDS:
                        break
                    async with batched_wakes():
                        dispatched, outcome = await _dispatch_poll_page(
                            db,
                            redis,
                            http,
                            wf=wf,
                            agent=agent,
                            provider=provider,
                            event_type=event_type,
                            access_token=access_token,
                            integration_id=integration_id,
                            route=route,
                        )
                    triggered += dispatched
                    if outcome in ("more", "caught_up", "seeded"):
                        await redis.set(f"{POLL_READ_KEY_PREFIX}{wf.id}", str(datetime.now(UTC).timestamp()), ex=86400)
                    if outcome != "more":
                        break
                if outcome == "refused":
                    break
                if outcome in ("caught_up", "seeded"):
                    await redis.set(poll_key, str(datetime.now(UTC).timestamp()), ex=86400)
                    if catch_up:
                        await redis.delete(catch_up_key)
                # ``owner_paused``: only this assignment is paused, and its poll
                # stamp is left alone like its cursor. ``queue_full`` / ``more``:
                # left unstamped, so the next tick reads on.

            except Exception as e:
                logger.warning("Event poll error for assignment %s: %r", wf.id, e, exc_info=True)

    return triggered


async def _poll_provider(
    provider: str,
    event_type: str,
    access_token: str,
    wf: Assignment,
    redis: Redis,
    seen_set_key: str,
    http: httpx.AsyncClient | None = None,
    *,
    integration_id: str | None = None,
) -> list[dict]:
    """Poll a specific provider for new events. Returns list of event payloads."""

    if provider == "microsoft" and event_type == "new_email":
        return await _poll_microsoft_emails(access_token, wf, redis, http, integration_id=integration_id)

    if provider == "microsoft" and event_type in ("new_teams_message", "new_teams_reply"):
        filter_config = (wf.trigger_config or {}).get("filter") or {}
        scope = filter_config.get("scope") or {}
        channels = scope.get("teams_channel") or filter_config.get("channels", [])
        if event_type == "new_teams_reply":
            return await _poll_microsoft_teams_replies(access_token, channels, wf, redis, http)
        return await _poll_microsoft_teams(access_token, channels, wf, redis, http)

    if provider == "microsoft" and event_type == "new_teams_chat_message":
        chat_ids = trigger_filter(getattr(wf, "trigger_config", None)).get("chat_ids") or []
        return await _poll_microsoft_chat_messages(access_token, chat_ids, wf, redis, http)

    if provider == "microsoft" and event_type == "calendar_event_changed":
        return await _poll_microsoft_calendar_changes(access_token, wf, redis, http)

    if provider == "microsoft" and event_type == "email_flag_changed":
        return await _poll_microsoft_flag_changes(access_token, wf, redis, http, integration_id=integration_id)

    if provider == "microsoft" and event_type == "todo_task_changed":
        return await _poll_microsoft_todo_changes(access_token, wf, redis, http)

    if provider == "slack" and event_type == "new_slack_message":
        channels = assignment_slack_channels(wf.trigger_config)
        return await _poll_slack_messages(access_token, channels, wf, redis, http)

    if provider == "google" and event_type == "new_email":
        return await _poll_google_emails(access_token, wf, redis, http)

    if provider == "docusign" and event_type in docusign.ENVELOPE_EVENTS:
        return await _poll_docusign_envelopes(access_token, event_type, wf, redis)

    if provider == opencrq_job_events.PROVIDER and event_type in opencrq_job_events.EVENTS:
        return await _poll_opencrq_job_runs(access_token, event_type, wf, redis)

    if provider == jira_events.PROVIDER and event_type in jira_events.EVENTS:
        return await _poll_jira_issues(access_token, event_type, wf, redis, integration_id=integration_id)

    if provider == quotapath_events.PROVIDER and event_type in quotapath_events.EVENTS:
        return await _poll_quotapath_payouts(access_token, event_type, wf, redis)

    if provider == odoo_events.PROVIDER and event_type in odoo_events.EVENTS:
        return await _poll_odoo_records(access_token, event_type, wf, redis)

    if provider == pennylane_events.PROVIDER and event_type in pennylane_events.EVENTS:
        return await _poll_pennylane_changes(access_token, event_type, wf, redis)

    if provider == dealhub_events.PROVIDER and dealhub_events.polled(event_type):
        return await _poll_dealhub_quotes(access_token, event_type, wf, redis)

    if provider == netsuite_events.PROVIDER and event_type in netsuite_events.EVENTS:
        return await _poll_netsuite_changes(access_token, event_type, wf, redis)

    # Unsupported provider/event — return empty
    logger.debug("No poller for %s/%s", provider, event_type)
    return []


#: Why the last poll of an assignment read nothing (a key per assignment, a day).
POLL_SKIP_KEY_PREFIX = "event_poll_skip:"
_POLL_SKIP_TTL_SECONDS = 86400


async def note_poll_skip(redis: Any, assignment_id: Any, reason: str | None) -> None:
    """Record (or clear, *reason* ``None``) why the poller skipped the assignment.

    ``graph_event_subscriptions.delivery_mode`` shows it next to the last poll,
    so a poll that never runs says why instead of "Last poll: never" alone.
    """
    key = f"{POLL_SKIP_KEY_PREFIX}{assignment_id}"
    try:
        if reason:
            await redis.set(key, reason[:300], ex=_POLL_SKIP_TTL_SECONDS)
        else:
            await redis.delete(key)
    except Exception as exc:
        logger.debug("Could not record the poll skip of assignment %s: %r", assignment_id, exc)


async def _poll_integration(db: Any, wf: Any, agent: Any, config: dict[str, Any]) -> tuple[Any, Any, str]:
    """``(integration_id, reader_id, "")`` the assignment is polled through, else ``(None, None, reason)``.

    The trigger's pinned integration or the agent's binding is read as the
    agent's owner, as before. An agent that reaches the provider only by
    template binding (the seeded agents) is polled through the integration
    its run-as user resolves at run time, read as that user; a seeded fleet
    automation with no run-as is never polled as the seed owner (#9188).
    """
    integration_id = trigger_integration_id(config, agent)
    if integration_id:
        return integration_id, agent_acting_user_id(agent), ""
    provider = str(config.get("provider") or "")
    from app.services.fleet_run_as import NO_RUN_AS_REASON, provisioning_user_id
    from app.services.trigger_provisioning import template_integration_id, uses_template

    if not uses_template(agent, provider):
        return None, None, f"no {provider} integration bound to the trigger or its agent"
    user_id = provisioning_user_id(wf, agent)
    if user_id is None:
        return None, None, NO_RUN_AS_REASON
    label = {"opencti": "OpenCTI", "openaev": "OpenAEV", "opencrq": "OpenCRQ"}.get(provider, provider)
    resolved, reason = await template_integration_id(db, agent, provider, label, user_id)
    if not resolved:
        return None, None, reason
    return resolved, user_id, ""


async def _poll_credential(
    provider: str,
    integration: Integration,
    holder: Any,
    *,
    db: AsyncSession | None = None,
    user_id: Any = None,
) -> str | None:
    """The credential a poll of *provider* reads with; ``None`` when there is none.

    The holder's access token, except for DocuSign, whose provider reads the
    compound credential the tools run on (``docusign.compound_credential``):
    the environment and the account ride with the OAuth token, and the JWT
    Grant mode stores no access token at all, only the RSA key it mints one with.
    """
    if (
        provider
        in (
            opencrq_job_events.PROVIDER,
            jira_events.PROVIDER,
            odoo_events.PROVIDER,
            pennylane_events.PROVIDER,
            netsuite_events.PROVIDER,
        )
        and db is not None
    ):
        # The compound the tools run on: OpenCRQ's ``base_url||token`` (an API
        # key, or a token minted for the acting user on a platform-managed
        # integration), Jira's ``cloud_id||token`` refreshed when about to expire,
        # Odoo's ``base_url||database||login||api_key[||company_ids]``,
        # Pennylane's token, prefixed ``company_id||`` for a firm token,
        # NetSuite's account, mode and secret (``netsuite.compound_credential``).
        from app.services.integration_tools import _get_token

        token = await _get_token(
            db, str(integration.id), provider, user_id=str(user_id) if user_id else None, integ=integration
        )
        if provider == pennylane_events.PROVIDER:
            return token or None
        return token if token and "||" in token else None
    if provider == dealhub_events.PROVIDER and db is not None:
        # ``host||token`` when the instance host is set, the bare token otherwise.
        from app.services.integration_tools import _get_token

        token = await _get_token(
            db, str(integration.id), provider, user_id=str(user_id) if user_id else None, integ=integration
        )
        return token or None
    if provider != "docusign":
        return decrypt_value(holder.access_token_enc) if holder.access_token_enc else None
    from app.services.vault import reveal_json_field

    try:
        extra = await reveal_json_field(integration, "extra_config_enc") if integration.extra_config_enc else {}
    except Exception as exc:
        logger.warning("Cannot read the DocuSign configuration of integration %s: %r", integration.id, exc)
        return None
    extra = extra if isinstance(extra, dict) else {}
    if integration.auth_type == "oauth":
        if not holder.access_token_enc:
            return None
        return docusign.compound_credential(decrypt_value(holder.access_token_enc), extra, jwt_grant=False)
    from app.services.vault import reveal_field

    return (
        docusign.compound_credential(
            (await reveal_field(integration, "api_key_enc")) or "",
            extra,
            jwt_grant=True,
            integration_key=decrypt_value(integration.client_id_enc) if integration.client_id_enc else "",
        )
        or None
    )


async def _poll_opencrq_job_runs(credential: str, event_type: str, wf: Assignment, redis: Redis) -> list[dict]:
    """Poll the OpenCRQ job runs that finished since the last check (``opencrq_job_events``).

    ``finishedFrom`` is inclusive: the run at the cursor comes back and the seen
    set skips it. The tenant is the trigger filter's ``tenant_id``, else the
    account's sole tenant. The first poll starts from now.

    The page is ONE event (``opencrq_job_events.coalesce_events``, #1417): a
    batch recompute starts one run, not one per job, and a run that processed
    nothing is no event. The runs a page covers that were already dispatched
    are left out, so a page fetched again starts nothing new.
    """
    cursor_key = f"event_cursor:{wf.id}"
    since = await redis.get(cursor_key)
    if not since:
        return await _seed_from_now(redis, cursor_key)
    since = since.decode() if isinstance(since, bytes) else str(since)
    tenant_id = str(trigger_filter(getattr(wf, "trigger_config", None)).get("tenant_id") or "").strip()
    runs, base_url, tenant, err = await opencrq_job_events.list_finished_runs(
        credential, event_type, since, tenant_id=tenant_id, limit=_OPENCRQ_PAGE + 1
    )
    if err:
        logger.warning("OpenCRQ job-run poll failed for assignment %s: %s", wf.id, err.splitlines()[0][:300])
        return []

    def finished(run: dict) -> datetime:
        return _iso_instant(run.get("finishedAt"))

    runs.sort(key=lambda r: (finished(r), str(r.get("id", ""))))
    page = _oldest_page(runs, finished, _OPENCRQ_PAGE)
    _log_page_cut(wf, "OpenCRQ", page)
    await _note_backlog(redis, wf, "OpenCRQ", page.more)
    if page.cursor is not None and page.cursor.get("finishedAt"):
        await redis.set(cursor_key, str(page.cursor["finishedAt"]), ex=_CURSOR_TTL_SECONDS)
    fresh = []
    for run in page.items:
        msg_id = opencrq_job_events.run_event(run, event_type, base_url=base_url, tenant_id=tenant)["message_id"]
        if msg_id and await redis.zscore(event_seen_set_key(wf.id), msg_id) is not None:
            continue
        fresh.append(run)
    return opencrq_job_events.coalesce_events(fresh, event_type, base_url=base_url, tenant_id=tenant)


async def _poll_quotapath_payouts(api_key: str, event_type: str, wf: Assignment, redis: Redis) -> list[dict[str, Any]]:
    """Poll the QuotaPath payouts of *event_type* since the last check (``quotapath.events``).

    The cursor is a Unix time in seconds: the updated-since lists read from it
    (inclusive, the seen set skipping a payout already run); the unresolved
    list has no time filter and the seen set alone tells a new payout. The
    first poll starts from now.
    """
    cursor_key = f"event_cursor:{wf.id}"
    since = await redis.get(cursor_key)
    now = int(datetime.now(UTC).timestamp())
    if not since:
        await redis.set(cursor_key, str(now), ex=_CURSOR_TTL_SECONDS)
        if event_type == "payout_unresolved":
            # Mark what is owed today seen, so enabling the trigger replays nothing.
            rows, err = await quotapath_events.list_payouts(api_key, event_type, None)
            for row in rows if not err else []:
                msg_id = quotapath_events.payout_event(row, event_type)["message_id"]
                if msg_id:
                    await claim_seen(redis, wf.id, msg_id)
        return []
    try:
        since_ts = int(since.decode() if isinstance(since, bytes) else since)
    except ValueError:
        since_ts = now
    rows, err = await quotapath_events.list_payouts(api_key, event_type, since_ts)
    if err:
        logger.warning("QuotaPath payout poll failed for assignment %s: %s", wf.id, err.splitlines()[0][:300])
        return []
    await redis.set(cursor_key, str(now), ex=_CURSOR_TTL_SECONDS)
    return [quotapath_events.payout_event(row, event_type) for row in rows]


async def _poll_odoo_records(credential: str, event_type: str, wf: Assignment, redis: Redis) -> list[dict[str, Any]]:
    """Poll the Odoo records of *event_type* stamped since the last check (``odoo.events``).

    The domain reads at or after the cursor (inclusive): the records at the
    cursor come back and the seen set skips them. The trigger filter narrows
    the search (company and journal ids, states). The first poll starts from now.
    """
    cursor_key = f"event_cursor:{wf.id}"
    since = await redis.get(cursor_key)
    if not since:
        await redis.set(cursor_key, odoo_events.now_cursor(), ex=_CURSOR_TTL_SECONDS)
        return []
    since = since.decode() if isinstance(since, bytes) else str(since)
    filter_config = trigger_filter(getattr(wf, "trigger_config", None))
    records, cred, err = await odoo_events.list_changed_records(
        credential, event_type, since, filter_config, limit=odoo_events.PAGE + 1
    )
    if err:
        logger.warning("Odoo poll failed for assignment %s: %s", wf.id, err.splitlines()[0][:300])
        return []

    def stamped(record: dict[str, Any]) -> str:
        return odoo_events.stamp_of(record, event_type)

    page = _oldest_page(records, stamped, odoo_events.PAGE)
    _log_page_cut(wf, "Odoo", page)
    await _note_backlog(redis, wf, "Odoo", page.more)
    if page.cursor is not None:
        await redis.set(cursor_key, stamped(page.cursor), ex=_CURSOR_TTL_SECONDS)
    return [odoo_events.record_event(record, event_type, credential=cred) for record in page.items]


async def _poll_pennylane_changes(
    credential: str, event_type: str, wf: Assignment, redis: Redis
) -> list[dict[str, Any]]:
    """Poll the Pennylane changelog entries processed since the last check (``pennylane_events``).

    ``start_date`` is inclusive: the entries at the cursor come back and the
    seen set skips them. The cursor moves past every entry of the page, the ones
    the event does not read included. The first poll starts from now.
    """
    cursor_key = f"event_cursor:{wf.id}"
    since = await redis.get(cursor_key)
    if not since:
        return await _seed_from_now(redis, cursor_key)
    since = since.decode() if isinstance(since, bytes) else str(since)
    entries, more, err = await pennylane_events.list_changes(credential, event_type, since, limit=_PENNYLANE_PAGE)
    if err:
        logger.warning("Pennylane changelog poll failed for assignment %s: %s", wf.id, err.splitlines()[0][:300])
        return []

    def processed(entry: dict[str, Any]) -> datetime:
        return pennylane_events.parse_instant(entry.get("processed_at"))

    entries.sort(key=lambda e: (processed(e), str(e.get("id", ""))))
    page = _oldest_page(entries, processed, _PENNYLANE_PAGE)
    _log_page_cut(wf, "Pennylane", page)
    await _note_backlog(redis, wf, "Pennylane", page.more or more)
    if page.cursor is not None and page.cursor.get("processed_at"):
        await redis.set(cursor_key, str(page.cursor["processed_at"]), ex=_CURSOR_TTL_SECONDS)
    return await pennylane_events.change_events(
        credential, event_type, pennylane_events.selected(page.items, event_type)
    )


async def _poll_dealhub_quotes(credential: str, event_type: str, wf: Assignment, redis: Redis) -> list[dict[str, Any]]:
    """Poll the DealHub quotes whose status changed since the last check (``dealhub_events``).

    The cursor is the status map of the quotes read last time; the first poll
    records it and runs nothing. A read that fails leaves the cursor alone.
    """
    cursor_key = f"event_cursor:{wf.id}"
    known = dealhub_events.load_statuses(await redis.get(cursor_key))
    quotes, err = await dealhub_events.list_quotes(credential)
    if err:
        logger.warning("DealHub quote poll failed for assignment %s: %s", wf.id, err.splitlines()[0][:300])
        return []
    delivered = await dealhub_events.webhook_statuses(redis, wf.id)
    events, statuses = dealhub_events.poll_events(quotes, event_type, known, delivered=delivered)
    await redis.set(cursor_key, dealhub_events.dump_statuses(statuses), ex=_CURSOR_TTL_SECONDS)
    return events


async def _poll_netsuite_changes(credential: str, event_type: str, wf: Assignment, redis: Redis) -> list[dict]:
    """Poll the NetSuite records changed since the last check (``netsuite_events``).

    Transactions: one SuiteQL read of the event's record types modified at or
    after the cursor (a NetSuite timestamp, never converted), oldest first; the
    record at the cursor comes back and the seen set skips it. Periods: the
    posting periods' flags diffed against the last snapshot. The first poll
    stores the cursor (the records at it marked seen) or the snapshot and runs
    nothing.
    """
    from app.integrations.providers.netsuite import parse_token, read_suiteql

    account_id = parse_token(credential).get("account_id") or ""
    cursor_key = f"event_cursor:{wf.id}"
    stored = await redis.get(cursor_key)
    if event_type == netsuite_events.ACCOUNTING_PERIOD_CHANGED:
        rows, _more, err = await read_suiteql(
            credential, netsuite_events.PERIODS_QUERY, 2000, "read accounting periods"
        )
        if err:
            logger.warning("NetSuite period poll failed for assignment %s: %s", wf.id, err.splitlines()[0][:300])
            return []
        previous = netsuite_events.load_snapshot(stored)
        snapshot = netsuite_events.period_snapshot(rows)
        if snapshot:
            await redis.set(cursor_key, netsuite_events.dump_snapshot(snapshot), ex=_CURSOR_TTL_SECONDS)
        if previous is None:
            return []
        return netsuite_events.period_events(rows, previous, account_id=account_id)
    since = stored.decode() if isinstance(stored, bytes) else (str(stored) if stored else "")
    if not netsuite_events.is_timestamp(since):
        # First poll (or an unreadable cursor): start from the newest change, run nothing.
        rows, _more, err = await read_suiteql(
            credential, netsuite_events.seed_query(event_type), 1, "read the newest change"
        )
        if err:
            logger.warning("NetSuite change poll failed for assignment %s: %s", wf.id, err.splitlines()[0][:300])
            return []
        seed = str((rows[0] if rows else {}).get("cursor") or "")
        if not netsuite_events.is_timestamp(seed):
            await redis.set(cursor_key, netsuite_events.EPOCH, ex=_CURSOR_TTL_SECONDS)
            return []
        # The next poll reads at or after the cursor, so the records modified at
        # the seed instant come back: they are marked seen now, never run.
        rows, _more, err = await read_suiteql(
            credential,
            netsuite_events.changes_query(event_type, None, seed),
            netsuite_events.PAGE + 1,
            "read the records at the newest change",
        )
        if err:
            logger.warning("NetSuite change poll failed for assignment %s: %s", wf.id, err.splitlines()[0][:300])
            return []
        for row in rows:
            if str(row.get("modified_at") or "") == seed:
                event = netsuite_events.transaction_event(row, event_type, account_id=account_id)
                if event["message_id"]:
                    await claim_seen(redis, wf.id, event["message_id"])
        await redis.set(cursor_key, seed, ex=_CURSOR_TTL_SECONDS)
        return []
    filter_config = trigger_filter(getattr(wf, "trigger_config", None))
    rows, more, err = await read_suiteql(
        credential,
        netsuite_events.changes_query(event_type, filter_config, since),
        netsuite_events.PAGE + 1,
        "read changed records",
    )
    if err:
        logger.warning("NetSuite change poll failed for assignment %s: %s", wf.id, err.splitlines()[0][:300])
        return []

    def modified(row: dict) -> str:
        # Same fixed-width format throughout: the text orders as the time does.
        return str(row.get("modified_at") or "")

    rows.sort(key=lambda r: (modified(r), str(r.get("id", ""))))
    page = _oldest_page(rows, modified, netsuite_events.PAGE)
    _log_page_cut(wf, "NetSuite", page)
    await _note_backlog(redis, wf, "NetSuite", page.more or more)
    if page.cursor is not None and netsuite_events.is_timestamp(modified(page.cursor)):
        await redis.set(cursor_key, modified(page.cursor), ex=_CURSOR_TTL_SECONDS)
    return [netsuite_events.transaction_event(row, event_type, account_id=account_id) for row in page.items]


#: The connected Jira account per integration (``GET /myself``), read once a day.
_JIRA_SELF_TTL_SECONDS = 86400


async def _jira_self_account_id(redis: Redis, integration_id: str | None, base: str, headers: dict) -> str:
    key = f"jira_self_account:{integration_id}"
    cached = await redis.get(key) if integration_id else None
    if cached:
        return cached.decode() if isinstance(cached, bytes) else str(cached)
    account = await jira_events.self_account_id(base, headers)
    if account and integration_id:
        await redis.set(key, account, ex=_JIRA_SELF_TTL_SECONDS)
    return account


async def _poll_jira_issues(
    credential: str, event_type: str, wf: Assignment, redis: Redis, *, integration_id: str | None = None
) -> list[dict]:
    """Poll the Jira issues changed since the last check (``jira_events``).

    One ``/search/jql`` of the issues updated (created, for ``issue_created``)
    since the cursor, narrowed by the trigger filter in JQL, with their
    changelog and comments; each change after the cursor is one event, oldest
    first. The filter's ``self`` and ``ignore_self_triggered_events`` read the
    connected account (``GET /myself``, cached a day). The first poll starts
    from now.
    """
    from app.integrations.providers.jira._common import jira_headers, parse_jira_token

    cursor_key = f"event_cursor:{wf.id}"
    since_raw = await redis.get(cursor_key)
    if not since_raw:
        return await _seed_from_now(redis, cursor_key)
    since_raw = since_raw.decode() if isinstance(since_raw, bytes) else str(since_raw)
    since = _iso_instant(since_raw)
    base, token = parse_jira_token(credential)
    if not base or not token:
        logger.warning("Jira poll for assignment %s: the integration has no Jira site (cloud_id)", wf.id)
        return []
    headers = jira_headers(token)
    filter_config = trigger_filter(getattr(wf, "trigger_config", None))
    issues, more, err = await jira_events.search_issues(
        base, headers, jira_events.poll_jql(event_type, filter_config, since)
    )
    if err:
        logger.warning("Jira issue poll failed for assignment %s: %s", wf.id, str(err).splitlines()[0][:300])
        return []
    events: list[dict] = []
    # Read back a margin before the cursor: a change Jira indexed late is
    # still found, and the seen set skips what already ran.
    for issue in issues:
        events.extend(jira_events.issue_events(issue, event_type, since=since - jira_events.WINDOW_MARGIN))

    def occurred(event: dict) -> datetime:
        return jira_events.parse_instant(event.get("occurred_at"))

    events.sort(key=lambda e: (occurred(e), str(e.get("message_id", ""))))
    page = _oldest_page(events, occurred, jira_events.SEARCH_PAGE)
    _log_page_cut(wf, "Jira", page)
    await _note_backlog(redis, wf, "Jira", page.more or more)
    if page.cursor is not None:
        await redis.set(
            cursor_key, occurred(page.cursor).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), ex=_CURSOR_TTL_SECONDS
        )
    elif not more:
        # Nothing changed: the cursor moves to now, so the window stays small.
        await redis.set(cursor_key, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), ex=_CURSOR_TTL_SECONDS)
    needs_self = getattr(wf, "ignore_self_triggered_events", False) or jira_events.names_self(filter_config)
    account = await _jira_self_account_id(redis, integration_id, base, headers) if needs_self and page.items else ""
    kept: list[dict] = []
    for event in page.items:
        if getattr(wf, "ignore_self_triggered_events", False) and jira_events.is_self_change(event, account):
            continue
        if account:
            event[FILTER_SIGNALS_KEY] = {"jira_self_account_id": account}
        kept.append(event)
    return kept


async def _poll_docusign_envelopes(token: str, event_type: str, wf: Assignment, redis: Redis) -> list[dict]:
    """Poll the DocuSign envelopes whose status changed since the last check.

    Reads ``GET /envelopes?from_date=<cursor>`` oldest first with the
    recipients, and turns each envelope into the events of *event_type* it
    carries (``docusign.envelope_events``). DocuSign's ``from_date`` is
    inclusive: the envelope at the cursor comes back and the seen set skips it.
    The first poll starts from now.
    """
    cursor_key = f"event_cursor:{wf.id}"
    since = await redis.get(cursor_key)
    if not since:
        return await _seed_from_now(redis, cursor_key)
    since = since.decode() if isinstance(since, bytes) else str(since)
    envelopes, environment, err = await docusign.list_envelope_changes(token, since, _DOCUSIGN_PAGE + 1)
    if err:
        logger.warning("DocuSign envelope poll failed for assignment %s: %s", wf.id, err.splitlines()[0][:300])
        return []

    def changed(envelope: dict) -> datetime:
        return docusign.event_instant(docusign.envelope_changed_at(envelope))

    envelopes.sort(key=lambda e: (changed(e), str(e.get("envelopeId", ""))))
    page = _oldest_page(envelopes, changed, _DOCUSIGN_PAGE)
    _log_page_cut(wf, "DocuSign", page)
    await _note_backlog(redis, wf, "DocuSign", page.more)
    if page.cursor is not None and docusign.envelope_changed_at(page.cursor):
        await redis.set(cursor_key, docusign.envelope_changed_at(page.cursor), ex=_CURSOR_TTL_SECONDS)
    events: list[dict] = []
    for envelope in page.items:
        events.extend(docusign.envelope_events(envelope, event_type, since=since, environment=environment))
    return events


async def _graph_mail_page(client: httpx.AsyncClient, token: str, url: str, wf: Assignment) -> dict | None:
    """One Graph ``/me/messages`` listing; ``None`` (logged) when Graph refuses it."""
    resp = await client.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if resp.status_code != 200:
        logger.warning(
            "Microsoft Graph /messages returned %d for assignment %s – body: %s | request-id: %s",
            resp.status_code,
            wf.id,
            resp.text[:500] or "(empty)",
            resp.headers.get("request-id", "?"),
        )
        return None
    return resp.json()


def _past_second(instant: datetime) -> str:
    """A cursor ``gt`` reads strictly after the whole second *instant* falls in."""
    return instant.strftime("%Y-%m-%dT%H:%M:%S") + ".999Z"


def _mail_listing(wf: Assignment) -> tuple[str, str]:
    """The Graph listing a mail trigger reads (its ``folder``, else every folder) and its ``$select``."""
    filter_config = trigger_filter(getattr(wf, "trigger_config", None))
    folder = filter_config.get("folder")
    base = (
        f"{GRAPH_API}/me/mailFolders/{quote(folder.strip(), safe='')}/messages"
        if isinstance(folder, str) and folder.strip()
        else f"{GRAPH_API}/me/messages"
    )
    select = _MICROSOFT_MAIL_SELECT
    if needs_mail_headers(filter_config):
        select += ",internetMessageHeaders"
    return base, select


def _graph_addresses(recipients: object) -> list[str]:
    return [
        address
        for recipient in (recipients if isinstance(recipients, list) else [])
        if isinstance(recipient, dict)
        and isinstance(address := (recipient.get("emailAddress") or {}).get("address"), str)
    ]


def _header_addresses(value: str) -> str:
    """The addresses of an RFC 5322 address header (``To``, ``Cc``), comma-joined, names dropped."""
    return ", ".join(address for _name, address in getaddresses([value]) if address)


def microsoft_mail_payload(msg: dict, *, event: str = "new_email", signals: bool = False) -> dict:
    """The trigger payload of a Graph mail, polled or notified: one shape for both.

    With *signals* (the trigger has a filter), what only the filter reads - the
    recipients, the headers, the resource type - rides under
    ``FILTER_SIGNALS_KEY`` and is removed before the run is created.
    """
    sender = (msg.get("from") or {}).get("emailAddress") or {}
    payload = {
        "from": sender.get("address", ""),
        "from_name": sender.get("name", ""),
        # The visible recipients, addresses comma-joined (``{{trigger.to}}``):
        # empty on a Bcc-only send, which Graph lists under bccRecipients alone.
        "to": ", ".join(_graph_addresses(msg.get("toRecipients"))),
        "cc": ", ".join(_graph_addresses(msg.get("ccRecipients"))),
        "subject": msg.get("subject", ""),
        "body": msg.get("bodyPreview", ""),
        "received_at": msg.get("receivedDateTime", ""),
        "is_flagged": (msg.get("flag") or {}).get("flagStatus") == "flagged",
        "message_id": msg.get("id", ""),
        # The thread: the mail trigger's ordering key.
        "conversation_id": msg.get("conversationId", ""),
        # The RFC 5322 Message-ID, the same in every mailbox the mail reached:
        # the run's dedup key across assignments (``services/event_run_dedup.py``).
        "internet_message_id": msg.get("internetMessageId", "") or "",
        # ``message`` or a meeting kind (``meetingRequest``, ``meetingCancelled``,
        # ``meetingResponse``), read from the ``@odata.type`` the listing carries.
        "message_type": message_type(msg),
        "trigger_type": "integration_event",
        "event": event,
        "provider": "microsoft",
    }
    if signals:
        payload[FILTER_SIGNALS_KEY] = {
            "to": _graph_addresses(msg.get("toRecipients")),
            "cc": _graph_addresses(msg.get("ccRecipients")),
            "headers": msg.get("internetMessageHeaders"),
            "resource_type": msg.get("@odata.type"),
        }
    return payload


async def _read_mail_second(
    client: httpx.AsyncClient,
    token: str,
    instant: datetime,
    wf: Assignment,
    listing: tuple[str, str] | None = None,
) -> list[dict] | None:
    """Every mail Graph received within the second of *instant*.

    Bounded by ``_MICROSOFT_MAIL_SECOND_MAX_PAGES``: past it, the rest of that
    second is not fetched, which is logged. ``None`` when Graph refuses a page.
    """
    base, select = listing or (f"{GRAPH_API}/me/messages", _MICROSOFT_MAIL_SELECT)
    start = instant.replace(microsecond=0)
    url: str | None = (
        f"{base}"
        f"?$top={_MICROSOFT_MAIL_SECOND_PAGE}"
        "&$orderby=receivedDateTime asc"
        f"&$select={select}"
        f"&$filter=receivedDateTime ge {start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f" and receivedDateTime lt {(start + timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    mails: list[dict] = []
    for _ in range(_MICROSOFT_MAIL_SECOND_MAX_PAGES):
        data = await _graph_mail_page(client, token, url, wf)
        if data is None:
            return None
        mails.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
        if not url:
            return mails
    _log_unfetched(wf, "Microsoft mail", f"has more than {len(mails)} new events received within one second")
    return mails


async def _poll_microsoft_emails(
    token: str,
    wf: Assignment,
    redis: Redis,
    http: httpx.AsyncClient | None = None,
    *,
    integration_id: str | None = None,
) -> list[dict]:
    """Poll Microsoft Graph for new emails since last check.

    ``/me/messages`` spans every folder, so a mail the agent sent or drafted through
    this mailbox would read as new: it is skipped. The recipients and
    ``sentDateTime`` are read for that check only. ``conversation_id`` is the
    thread, the default ordering key of a mail trigger.
    """

    # The cursor is the receivedDateTime of the last email dispatched. After it,
    # the oldest first, one more than the page to see what follows it; a
    # first poll reads the latest page.
    cursor_key = f"event_cursor:{wf.id}"
    last_seen_dt = await redis.get(cursor_key)

    if last_seen_dt:
        order, top = "asc", _MICROSOFT_MAIL_PAGE + 1
    else:
        order, top = "desc", _MICROSOFT_MAIL_PAGE
    listing = _mail_listing(wf)
    base, select = listing
    url = (
        f"{base}"
        f"?$top={top}"
        f"&$orderby=receivedDateTime {order}"
        f"&$select={select}"
    )
    if last_seen_dt:
        url += f"&$filter=receivedDateTime gt {last_seen_dt}"

    client = http or egress_client()
    try:
        data = await _graph_mail_page(client, token, url, wf)
        if data is None:
            return []
        messages = data.get("value", [])
        if not messages:
            # No new messages — if first poll, set cursor to now
            if not last_seen_dt:
                await redis.set(
                    cursor_key,
                    datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    ex=_CURSOR_TTL_SECONDS,
                )
            return []

        messages.sort(key=lambda msg: (_mail_received_at(msg), msg.get("id", "")))
        if last_seen_dt:
            page = _oldest_page(messages, _mail_received_at, _MICROSOFT_MAIL_PAGE)
            messages = page.items
            cursor_dt = page.cursor.get("receivedDateTime", "")
            more = page.more
            stop = _mail_received_at(page.cursor)
            # The cursor would stay on the second it holds, or stop on a second
            # whose mails go past the page: ``gt`` that second returns its mails
            # again (Graph compares their sub-second value), so the poll would
            # read the same page forever. That second is read whole.
            if cursor_dt and (page.cut or stop == _iso_instant(last_seen_dt).replace(microsecond=0)):
                second = await _read_mail_second(client, token, stop, wf, listing)
                if second is None:
                    _log_page_cut(wf, "Microsoft mail", page)
                else:
                    earlier = [msg for msg in messages if _mail_received_at(msg) < stop]
                    at_stop = {msg.get("id", ""): msg for msg in messages if _mail_received_at(msg) == stop}
                    at_stop.update((msg.get("id", ""), msg) for msg in second)
                    messages = earlier + [at_stop[msg_id] for msg_id in sorted(at_stop)]
                    cursor_dt = _past_second(stop)
                    more = True
            await _note_backlog(redis, wf, "Microsoft mail", more)
        else:
            cursor_dt = messages[-1].get("receivedDateTime", "")
    finally:
        if not http:
            await client.aclose()
    if cursor_dt:
        await redis.set(cursor_key, cursor_dt, ex=_CURSOR_TTL_SECONDS)

    messages = [msg for msg in messages if not await is_agent_mail(redis, integration_id, msg, wf.id)]

    # Dispatched oldest first.
    signals = bool(trigger_filter(getattr(wf, "trigger_config", None)))
    return [microsoft_mail_payload(msg, signals=signals) for msg in messages]


async def _teams_channel_targets(
    client: httpx.AsyncClient, headers: dict, channels: list[str], wf: Assignment
) -> list[tuple[str, str, str]] | None:
    """``(team_id, channel_id, label)`` of the channels a Teams trigger reads.

    The configured ``team_id:channel_id`` entries, else the channels of the
    user's joined teams (capped at ``_MAX_DISCOVERED_CHANNELS``). ``None`` when
    Graph refuses the discovery.
    """
    channel_targets: list[tuple[str, str, str]] = []
    if channels:
        for ch in channels:
            parts = ch.split(":", 1)
            if len(parts) == 2:
                channel_targets.append((parts[0], parts[1], ch))
        return channel_targets
    resp = await client.get(
        f"{GRAPH_API}/me/joinedTeams",
        headers=headers,
        params={"$select": "id,displayName"},
        timeout=20,
    )
    if resp.status_code != 200:
        logger.warning(
            "Graph /me/joinedTeams returned %d for assignment %s – %s",
            resp.status_code,
            wf.id,
            resp.text[:300],
        )
        return None
    for team in resp.json().get("value", []):
        team_id = team.get("id", "")
        team_name = team.get("displayName", "?")
        ch_resp = await client.get(
            f"{GRAPH_API}/teams/{team_id}/channels",
            headers=headers,
            params={"$select": "id,displayName"},
            timeout=20,
        )
        if ch_resp.status_code >= 400:
            continue
        for ch in ch_resp.json().get("value", []):
            ch_id = ch.get("id", "")
            label = f"{team_name}/{ch.get('displayName', '?')}"
            channel_targets.append((team_id, ch_id, label))
            if len(channel_targets) >= _MAX_DISCOVERED_CHANNELS:
                break
        if len(channel_targets) >= _MAX_DISCOVERED_CHANNELS:
            break
    return channel_targets


def _graph_author(msg: dict) -> tuple[str, str]:
    """``(display name, user id)`` of a Teams message's author; empty for an app or system."""
    user = (msg.get("from") or {}).get("user") or {}
    return user.get("displayName", "") or "", user.get("id", "") or ""


async def _poll_microsoft_teams(
    token: str,
    channels: list[str],
    wf: Assignment,
    redis: Redis,
    http: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Poll Microsoft Graph for new Teams channel messages.

    Uses per-channel endpoints (``/teams/{id}/channels/{id}/messages``)
    which work with delegated OAuth tokens.  The previous implementation
    used ``/me/chats/getAllMessages`` which requires application-only
    permissions and always returns 412 in delegated context.

    Channel format from the element picker: ``"team_id:channel_id"``.
    When no channels are configured, discovers all channels from the
    user's joined teams (capped at ``_MAX_DISCOVERED_CHANNELS``).
    """

    cursor_key = f"event_cursor:{wf.id}"
    last_seen_dt = await redis.get(cursor_key)

    client = http or egress_client()
    headers = {"Authorization": f"Bearer {token}"}

    try:
        # -- resolve channel targets -----------------------------------------
        channel_targets = await _teams_channel_targets(client, headers, channels, wf)
        if channel_targets is None:
            return []

        if not channel_targets:
            if not last_seen_dt:
                await redis.set(
                    cursor_key,
                    datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    ex=_CURSOR_TTL_SECONDS,
                )
            return []

        # -- poll messages per channel ----------------------------------------
        # Graph lists a channel's latest messages first and cannot sort them: after
        # the cursor, each channel is read back to it, and the oldest page across
        # the channels is dispatched. A first poll reads the latest page.
        since = _iso_instant(last_seen_dt) if last_seen_dt else None
        fetched: list[tuple[str, str, str, dict]] = []
        for team_id, channel_id, label in channel_targets:
            url: str | None = f"{GRAPH_API}/teams/{team_id}/channels/{channel_id}/messages"
            params: dict | None = {"$top": str(_TEAMS_LIST_PAGE if since else _CHANNEL_MESSAGE_PAGE)}
            pages = read = 0
            while url:
                resp = await client.get(url, headers=headers, params=params, timeout=30)
                if resp.status_code != 200:
                    logger.debug(
                        "Teams channel %s returned %d – skipping",
                        label,
                        resp.status_code,
                    )
                    if pages:
                        _log_unfetched(wf, f"Teams channel {label}", "could not be read back to its cursor")
                    break
                data = resp.json()
                pages += 1
                listed = data.get("value", [])
                read += len(listed)
                reached = since is None
                for msg in listed:
                    # Whichever of the two dates the listing is sorted by, a message
                    # changed last before the cursor has nothing new after it.
                    if since is not None and _teams_touched_at(msg) <= since:
                        reached = True
                    if since is None or _teams_created_at(msg) > since:
                        fetched.append((team_id, channel_id, label, msg))
                url = data.get("@odata.nextLink")
                if reached or not url:
                    break
                if pages >= _TEAMS_MAX_LIST_PAGES:
                    _log_unfetched(wf, f"Teams channel {label}", f"has more than the {read} messages a poll reads")
                    break
                params = None

        fetched.sort(key=lambda item: (_teams_created_at(item[3]), item[3].get("id", "")))
        cursor_dt = ""
        if since is not None:
            page = _oldest_page(fetched, lambda item: _teams_created_at(item[3]), _CHANNEL_MESSAGE_PAGE)
            fetched = page.items
            _log_page_cut(wf, "Teams", page)
            await _note_backlog(redis, wf, "Teams", page.more)
            if page.cursor is not None:
                cursor_dt = page.cursor[3].get("createdDateTime", "")
        elif fetched:
            cursor_dt = fetched[-1][3].get("createdDateTime", "")

        events: list[dict] = []
        signals = bool(trigger_filter(getattr(wf, "trigger_config", None)))
        for team_id, channel_id, label, msg in fetched:
            if msg.get("messageType", "") != "message":
                continue
            if await is_agent_teams_post(redis, team_id, channel_id, msg.get("id", ""), wf.id):
                continue
            event = {
                "text": (msg.get("body") or {}).get("content", ""),
                "from_name": _graph_author(msg)[0],
                "channel_name": label,
                "team_id": team_id,
                "channel_id": channel_id,
                "created_at": msg.get("createdDateTime", ""),
                "message_id": msg.get("id", ""),
                "trigger_type": "integration_event",
                "event": "new_teams_message",
                "provider": "microsoft",
            }
            if signals:
                event[FILTER_SIGNALS_KEY] = {"from_id": _graph_author(msg)[1]}
            events.append(event)

        # The cursor moves past every message read, a skipped one included.
        if cursor_dt:
            await redis.set(cursor_key, cursor_dt, ex=_CURSOR_TTL_SECONDS)
        elif not last_seen_dt:
            await redis.set(
                cursor_key,
                datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ex=_CURSOR_TTL_SECONDS,
            )

        # Dispatched oldest first, across channels.
        return events
    finally:
        if not http:
            await client.aclose()


def _graph_filter_instant(instant: datetime) -> str:
    """A Graph ``$filter`` timestamp (milliseconds, UTC)."""
    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


async def _seed_from_now(redis: Redis, cursor_key: str) -> list[dict]:
    """A trigger of the event types added with Graph subscriptions starts from now:
    its first poll runs nothing, so enabling one never replays the history."""
    await redis.set(cursor_key, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), ex=_CURSOR_TTL_SECONDS)
    return []


def teams_reply_payload(
    reply: dict,
    *,
    team_id: str,
    channel_id: str,
    channel_name: str,
    parent_id: str,
    signals: bool = False,
) -> dict:
    """The trigger payload of a reply in a Teams channel thread, polled or notified."""
    name, user_id = _graph_author(reply)
    payload = {
        "text": (reply.get("body") or {}).get("content", ""),
        "from_name": name,
        "channel_name": channel_name,
        "team_id": team_id,
        "channel_id": channel_id,
        "created_at": reply.get("createdDateTime", ""),
        "message_id": reply.get("id", ""),
        "parent_message_id": parent_id or reply.get("replyToId", "") or "",
        "trigger_type": "integration_event",
        "event": "new_teams_reply",
        "provider": "microsoft",
    }
    # One turn per thread: the replies of one thread run one at a time
    # and a sender's rapid replies coalesce (``services/chat_turn_coalescing.py``).
    thread = payload["parent_message_id"]
    if thread:
        payload[CONVERSATION_KEY_FIELD] = thread_conversation_key(team_id, channel_id, thread)
    if signals:
        payload[FILTER_SIGNALS_KEY] = {"from_id": user_id}
    return payload


async def _poll_microsoft_teams_replies(
    token: str,
    channels: list[str],
    wf: Assignment,
    redis: Redis,
    http: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Poll the replies posted in Teams channel threads since the last check.

    Reads, per channel, its latest ``_TEAMS_REPLY_THREADS`` threads with their
    replies (``$expand=replies``) and dispatches the replies created after the
    cursor, oldest first. A reply to an older thread is past what a poll reads
    (a Graph subscription notifies every reply). The first poll starts from now.
    """
    cursor_key = f"event_cursor:{wf.id}"
    last_seen_dt = await redis.get(cursor_key)
    if not last_seen_dt:
        return await _seed_from_now(redis, cursor_key)
    since = _iso_instant(last_seen_dt)
    client = http or egress_client()
    headers = {"Authorization": f"Bearer {token}"}
    fetched: list[tuple[str, str, str, str, dict]] = []
    try:
        targets = await _teams_channel_targets(client, headers, channels, wf)
        for team_id, channel_id, label in targets or []:
            resp = await client.get(
                f"{GRAPH_API}/teams/{team_id}/channels/{channel_id}/messages",
                headers=headers,
                params={"$top": str(_TEAMS_REPLY_THREADS), "$expand": "replies"},
                timeout=30,
            )
            if resp.status_code != 200:
                logger.debug("Teams channel %s returned %d for its replies – skipping", label, resp.status_code)
                continue
            for root in resp.json().get("value", []):
                for reply in root.get("replies") or []:
                    if isinstance(reply, dict) and _teams_created_at(reply) > since:
                        fetched.append((team_id, channel_id, label, root.get("id", ""), reply))
    finally:
        if not http:
            await client.aclose()

    fetched.sort(key=lambda item: (_teams_created_at(item[4]), item[4].get("id", "")))
    page = _oldest_page(fetched, lambda item: _teams_created_at(item[4]), _CHANNEL_MESSAGE_PAGE)
    _log_page_cut(wf, "Teams replies", page)
    await _note_backlog(redis, wf, "Teams replies", page.more)
    if page.cursor is not None:
        await redis.set(cursor_key, page.cursor[4].get("createdDateTime", ""), ex=_CURSOR_TTL_SECONDS)

    signals = bool(trigger_filter(getattr(wf, "trigger_config", None)))
    events: list[dict] = []
    for team_id, channel_id, label, parent_id, reply in page.items:
        if reply.get("messageType", "") != "message":
            continue
        if await is_agent_teams_post(redis, team_id, channel_id, reply.get("id", ""), wf.id):
            continue
        events.append(
            teams_reply_payload(
                reply,
                team_id=team_id,
                channel_id=channel_id,
                channel_name=label,
                parent_id=parent_id,
                signals=signals,
            )
        )
    return events


def teams_chat_payload(msg: dict, *, chat_id: str, chat_topic: str = "", chat_type: str = "", signals: bool = False) -> dict:
    """The trigger payload of a Teams chat message (1:1 or group), polled or notified."""
    name, user_id = _graph_author(msg)
    payload = {
        "text": (msg.get("body") or {}).get("content", ""),
        "from_name": name,
        "chat_id": chat_id,
        "chat_topic": chat_topic or "",
        "chat_type": chat_type or msg.get("chatType", "") or "",
        "created_at": msg.get("createdDateTime", ""),
        "message_id": msg.get("id", ""),
        "trigger_type": "integration_event",
        "event": "new_teams_chat_message",
        "provider": "microsoft",
    }
    # One turn per chat: its messages run one at a time and a sender's
    # rapid messages coalesce (``services/chat_turn_coalescing.py``).
    if chat_id:
        payload[CONVERSATION_KEY_FIELD] = chat_conversation_key(chat_id)
    if signals:
        payload[FILTER_SIGNALS_KEY] = {"from_id": user_id}
    return payload


def _chat_segment(chat_id: str) -> str:
    return quote(chat_id, safe=":@.")


async def _poll_microsoft_chat_messages(
    token: str,
    chat_ids: list[str],
    wf: Assignment,
    redis: Redis,
    http: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Poll new messages of the user's Teams chats (1:1 and group) since the last check.

    The chats of the trigger's ``chat_ids``, else those whose last message is
    newer than the cursor (``/me/chats``, at most ``_MAX_DISCOVERED_CHATS``).
    The first poll starts from now.
    """
    cursor_key = f"event_cursor:{wf.id}"
    last_seen_dt = await redis.get(cursor_key)
    if not last_seen_dt:
        return await _seed_from_now(redis, cursor_key)
    since = _iso_instant(last_seen_dt)
    client = http or egress_client()
    headers = {"Authorization": f"Bearer {token}"}
    fetched: list[tuple[str, str, str, dict]] = []
    try:
        targets: list[tuple[str, str, str]] = [
            (chat_id, "", "") for chat_id in chat_ids if isinstance(chat_id, str) and chat_id.strip()
        ]
        if not targets:
            resp = await client.get(
                f"{GRAPH_API}/me/chats",
                headers=headers,
                params={"$top": str(_CHAT_LIST_PAGE), "$expand": "lastMessagePreview"},
                timeout=20,
            )
            if resp.status_code != 200:
                logger.warning(
                    "Graph /me/chats returned %d for assignment %s – %s", resp.status_code, wf.id, resp.text[:300]
                )
                return []
            for chat in resp.json().get("value", []):
                preview = chat.get("lastMessagePreview") or {}
                if _iso_instant(preview.get("createdDateTime")) > since:
                    targets.append((chat.get("id", ""), chat.get("topic") or "", chat.get("chatType") or ""))
                if len(targets) >= _MAX_DISCOVERED_CHATS:
                    break
        for chat_id, topic, chat_type in targets:
            resp = await client.get(
                f"{GRAPH_API}/chats/{_chat_segment(chat_id)}/messages",
                headers=headers,
                params={
                    "$top": str(_CHAT_LIST_PAGE),
                    "$orderby": "lastModifiedDateTime desc",
                    "$filter": f"lastModifiedDateTime gt {_graph_filter_instant(since)}",
                },
                timeout=30,
            )
            if resp.status_code != 200:
                logger.debug("Teams chat %s returned %d – skipping", chat_id, resp.status_code)
                continue
            for msg in resp.json().get("value", []):
                if isinstance(msg, dict) and _teams_created_at(msg) > since:
                    fetched.append((chat_id, topic, chat_type, msg))
    finally:
        if not http:
            await client.aclose()

    fetched.sort(key=lambda item: (_teams_created_at(item[3]), item[3].get("id", "")))
    page = _oldest_page(fetched, lambda item: _teams_created_at(item[3]), _CHANNEL_MESSAGE_PAGE)
    _log_page_cut(wf, "Teams chats", page)
    await _note_backlog(redis, wf, "Teams chats", page.more)
    if page.cursor is not None:
        await redis.set(cursor_key, page.cursor[3].get("createdDateTime", ""), ex=_CURSOR_TTL_SECONDS)

    signals = bool(trigger_filter(getattr(wf, "trigger_config", None)))
    events: list[dict] = []
    for chat_id, topic, chat_type, msg in page.items:
        if msg.get("messageType", "") != "message":
            continue
        if await is_agent_teams_chat_post(redis, chat_id, msg.get("id", ""), wf.id):
            continue
        events.append(teams_chat_payload(msg, chat_id=chat_id, chat_topic=topic, chat_type=chat_type, signals=signals))
    return events


def calendar_change_kind(event: dict, change_type: str = "") -> str:
    """``created``, ``updated`` or ``cancelled`` for a calendar event and its Graph change type.

    A deletion and a cancelled meeting are ``cancelled``; an event first modified
    within ``_CREATED_WITHIN`` of its creation is ``created``.
    """
    if change_type == "deleted" or event.get("isCancelled"):
        return "cancelled"
    if change_type == "created":
        return "created"
    created = _iso_instant(event.get("createdDateTime"))
    modified = _iso_instant(event.get("lastModifiedDateTime"))
    if change_type != "updated" and modified - created <= _CREATED_WITHIN:
        return "created"
    return "updated"


def calendar_event_payload(event: dict, *, change_type: str = "") -> dict:
    """The trigger payload of a changed calendar event, polled or notified.

    ``message_id`` names this change (the event and when it changed), so each
    change of one event runs once; ``event_id`` names the event itself.
    """
    start = event.get("start") or {}
    end = event.get("end") or {}
    organizer = (event.get("organizer") or {}).get("emailAddress") or {}
    event_id = event.get("id", "")
    modified = event.get("lastModifiedDateTime", "") or datetime.now(UTC).isoformat()
    kind = calendar_change_kind(event, change_type)
    attendees = sorted(
        str(((a or {}).get("emailAddress") or {}).get("address") or "").lower()
        for a in (event.get("attendees") or [])
        if isinstance(a, dict)
    )
    # What an agent acts on, without the save instant: two saves that change
    # none of it share the digest, so the second is a duplicate run (#2216).
    content = json.dumps(
        [
            start.get("dateTime", ""),
            end.get("dateTime", ""),
            start.get("timeZone", ""),
            kind in ("cancelled", "deleted"),
            kind == "deleted",
            event.get("subject", ""),
            (event.get("location") or {}).get("displayName", ""),
            attendees,
        ],
        separators=(",", ":"),
    )
    return {
        "event_id": event_id,
        "change_kind": kind,
        "subject": event.get("subject", ""),
        "start": start.get("dateTime", ""),
        "end": end.get("dateTime", ""),
        "time_zone": start.get("timeZone", ""),
        "location": (event.get("location") or {}).get("displayName", ""),
        "organizer": organizer.get("address", ""),
        "organizer_name": organizer.get("name", ""),
        "is_cancelled": kind == "cancelled",
        "series_master_id": event.get("seriesMasterId", "") or "",
        # The event across calendars (an attendee's copy and the organizer's
        # share it): with ``last_modified_at``, the run's dedup key.
        "ical_uid": event.get("iCalUId", "") or "",
        "last_modified_at": event.get("lastModifiedDateTime", ""),
        # Digest of the time, cancellation, subject, location and attendees:
        # with ``ical_uid``, the run's dedup key.
        "content_digest": hashlib.sha256(content.encode()).hexdigest()[:16],
        "web_link": event.get("webLink", ""),
        "message_id": f"{event_id}@{modified}" if event_id else "",
        "trigger_type": "integration_event",
        "event": "calendar_event_changed",
        "provider": "microsoft",
    }


async def _poll_microsoft_calendar_changes(
    token: str, wf: Assignment, redis: Redis, http: httpx.AsyncClient | None = None
) -> list[dict]:
    """Poll the mailbox's calendar events created, updated or cancelled since the last check.

    Reads ``/me/events`` by ``lastModifiedDateTime``, oldest first. An event
    deleted outright leaves nothing to read back: only a Graph subscription
    notifies it. The first poll starts from now.
    """
    cursor_key = f"event_cursor:{wf.id}"
    last_seen_dt = await redis.get(cursor_key)
    if not last_seen_dt:
        return await _seed_from_now(redis, cursor_key)
    client = http or egress_client()
    try:
        resp = await client.get(
            f"{GRAPH_API}/me/events",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "$top": str(_CALENDAR_PAGE + 1),
                "$orderby": "lastModifiedDateTime asc",
                "$filter": f"lastModifiedDateTime gt {last_seen_dt}",
                "$select": _CALENDAR_SELECT,
            },
            timeout=30,
        )
    finally:
        if not http:
            await client.aclose()
    if resp.status_code != 200:
        logger.warning(
            "Microsoft Graph /me/events returned %d for assignment %s – %s", resp.status_code, wf.id, resp.text[:300]
        )
        return []
    changed = [e for e in resp.json().get("value", []) if isinstance(e, dict)]
    changed.sort(key=lambda e: (_iso_instant(e.get("lastModifiedDateTime")), e.get("id", "")))
    page = _oldest_page(changed, lambda e: _iso_instant(e.get("lastModifiedDateTime")), _CALENDAR_PAGE)
    _log_page_cut(wf, "Microsoft calendar", page)
    await _note_backlog(redis, wf, "Microsoft calendar", page.more)
    if page.cursor is not None:
        await redis.set(cursor_key, page.cursor.get("lastModifiedDateTime", ""), ex=_CURSOR_TTL_SECONDS)
    return [calendar_event_payload(event) for event in page.items]


def flag_change_payload(msg: dict, previous: str | None, *, signals: bool = False) -> dict:
    """The trigger payload of a mail whose follow-up flag changed, polled or notified.

    The mail's fields as ``new_email`` reads them, ``mail_id`` the mail itself
    and ``message_id`` this change (mail, new status, when).
    """
    status = (msg.get("flag") or {}).get("flagStatus") or "notFlagged"
    payload = microsoft_mail_payload(msg, event="email_flag_changed", signals=signals)
    payload.update(
        {
            "mail_id": msg.get("id", ""),
            "flag_status": status,
            "previous_flag_status": previous or "",
            "flag_due_at": ((msg.get("flag") or {}).get("dueDateTime") or {}).get("dateTime", ""),
            "message_id": f"{msg.get('id', '')}:{status}:{msg.get('lastModifiedDateTime', '')}" if msg.get("id") else "",
        }
    )
    return payload


async def flag_changes(redis: Redis, assignment_id: object, messages: list[dict]) -> list[tuple[dict, str | None]]:
    """The mails of *messages* whose flag status differs from the one this
    assignment last saw, with that previous status, and the new statuses kept.

    A mail never seen before counts as changed when it carries a flag (the
    default status is ``notFlagged``).
    """
    key = f"event_flag_state:{assignment_id}"
    ids = [m.get("id", "") for m in messages if m.get("id")]
    known: dict[str, str | None] = {}
    if ids:
        try:
            for msg_id, value in zip(ids, await redis.hmget(key, ids), strict=True):
                known[msg_id] = value.decode() if isinstance(value, bytes) else value
        except Exception as exc:
            logger.debug("Could not read the flag states of assignment %s: %r", assignment_id, exc)
    changes: list[tuple[dict, str | None]] = []
    seen: dict[str, str] = {}
    for msg in messages:
        msg_id = msg.get("id", "")
        if not msg_id:
            continue
        status = (msg.get("flag") or {}).get("flagStatus") or "notFlagged"
        previous = seen.get(msg_id, known.get(msg_id))
        if (previous is None and status != "notFlagged") or (previous is not None and previous != status):
            changes.append((msg, previous))
        seen[msg_id] = status
    if seen:
        try:
            await redis.hset(key, mapping=seen)
            await redis.expire(key, _FLAG_STATE_TTL_SECONDS)
        except Exception as exc:
            logger.debug("Could not keep the flag states of assignment %s: %r", assignment_id, exc)
    return changes


def todo_task_payload(task: dict, *, list_id: str, list_name: str, change_kind: str, signals: bool = False) -> dict:
    """The trigger payload of a changed To Do task, polled or notified.

    ``message_id`` names this change (the task and when it changed), so each
    change runs once; ``task_id`` and ``list_id`` name the task to act on.
    """
    task_id = str(task.get("id") or "")
    modified = str(task.get("lastModifiedDateTime") or "") or datetime.now(UTC).isoformat()
    payload = {
        "task_id": task_id,
        "list_id": list_id,
        "list_name": list_name,
        "change_kind": change_kind,
        "title": task.get("title", "") or "",
        "body": ((task.get("body") or {}).get("content") or "") if isinstance(task.get("body"), dict) else "",
        "status": task.get("status", "") or "",
        "importance": task.get("importance", "") or "",
        "due_date": ((task.get("dueDateTime") or {}).get("dateTime") or "")[:10],
        "created_at": task.get("createdDateTime", "") or "",
        "last_modified_at": task.get("lastModifiedDateTime", "") or "",
        "message_id": f"{task_id}@{modified}:{change_kind}" if task_id else "",
        "trigger_type": "integration_event",
        "event": "todo_task_changed",
        "provider": "microsoft",
    }
    if signals:
        payload[FILTER_SIGNALS_KEY] = {"list_wellknown": task.get("_list_wellknown", "")}
    return payload


async def todo_change_kind(redis: Redis, assignment_id: object, task: dict, *, deleted: bool = False) -> str:
    """``created`` / ``updated`` / ``completed`` / ``deleted`` for one task change, the
    status this assignment last saw kept (a task never seen counts as created)."""
    key = f"event_todo_state:{assignment_id}"
    task_id = str(task.get("id") or "")
    status = str(task.get("status") or "notStarted")
    previous: str | None = None
    try:
        raw = await redis.hget(key, task_id) if task_id else None
        previous = raw.decode() if isinstance(raw, bytes) else raw
        if task_id:
            if deleted:
                await redis.hdel(key, task_id)
            else:
                await redis.hset(key, task_id, status)
                await redis.expire(key, _FLAG_STATE_TTL_SECONDS)
    except Exception as exc:
        logger.debug("Could not keep the To Do task states of assignment %s: %r", assignment_id, exc)
    if deleted:
        return "deleted"
    if status == "completed" and previous != "completed":
        return "completed"
    if previous is None:
        created = _iso_instant(task.get("createdDateTime"))
        modified = _iso_instant(task.get("lastModifiedDateTime"))
        return "created" if modified - created <= _CREATED_WITHIN else "updated"
    return "updated"


def _todo_list_selected(todo_list: dict, filter_config: dict) -> bool:
    """Whether the trigger's ``lists`` / ``exclude_lists`` keep *todo_list* (by id or name)."""
    from app.services.integration_event_filters import todo_list_matches

    return todo_list_matches(
        str(todo_list.get("id") or ""),
        str(todo_list.get("displayName") or ""),
        str(todo_list.get("wellknownListName") or ""),
        filter_config,
    )


async def is_own_todo_change(redis: Redis, wf: Assignment, task: dict) -> bool:
    """A change the assignment's own run made, or (``exclude_self``) any platform write."""
    from app.services.agent_posts import is_own_todo_write

    exclude_self = bool(trigger_filter(getattr(wf, "trigger_config", None)).get("exclude_self"))
    return await is_own_todo_write(
        redis, task.get("id"), task.get("lastModifiedDateTime"), wf.id, exclude_self=exclude_self
    )


async def _poll_microsoft_todo_changes(
    token: str, wf: Assignment, redis: Redis, http: httpx.AsyncClient | None = None
) -> list[dict]:
    """Poll the To Do tasks created, updated or completed since the last check.

    Reads the task lists the filter keeps, then each list's tasks modified after
    the cursor; one cursor across the lists, oldest change first. A task
    deleted outright leaves nothing to read back: only a Graph subscription
    notifies it. The first poll starts from now.
    """
    cursor_key = f"event_cursor:{wf.id}"
    last_seen = await redis.get(cursor_key)
    if not last_seen:
        return await _seed_from_now(redis, cursor_key)
    if isinstance(last_seen, bytes):
        last_seen = last_seen.decode()
    since = _iso_instant(last_seen)
    filter_config = trigger_filter(getattr(wf, "trigger_config", None))
    headers = {"Authorization": f"Bearer {token}"}
    client = http or egress_client()
    changed: list[dict] = []
    try:
        resp = await client.get(
            f"{GRAPH_API}/me/todo/lists", headers=headers, params={"$top": str(_TODO_MAX_LISTS)}, timeout=30
        )
        if resp.status_code != 200:
            logger.warning(
                "Microsoft Graph /me/todo/lists returned %d for assignment %s – %s",
                resp.status_code,
                wf.id,
                resp.text[:300],
            )
            return []
        lists = [tl for tl in resp.json().get("value", []) if isinstance(tl, dict) and tl.get("id")]
        for todo_list in lists:
            if not _todo_list_selected(todo_list, filter_config):
                continue
            list_id = str(todo_list["id"])
            tasks_resp = await client.get(
                f"{GRAPH_API}/me/todo/lists/{quote(list_id, safe='')}/tasks",
                headers=headers,
                params={
                    "$top": str(_TODO_PAGE + 1),
                    "$filter": f"lastModifiedDateTime gt {_graph_filter_instant(since)}",
                    "$select": _TODO_SELECT,
                },
                timeout=30,
            )
            if tasks_resp.status_code != 200:
                logger.warning(
                    "Microsoft Graph To Do tasks of a list returned %d for assignment %s – %s",
                    tasks_resp.status_code,
                    wf.id,
                    tasks_resp.text[:300],
                )
                continue
            for task in tasks_resp.json().get("value", []):
                if isinstance(task, dict) and _iso_instant(task.get("lastModifiedDateTime")) > since:
                    task["_list_id"] = list_id
                    task["_list_name"] = str(todo_list.get("displayName") or "")
                    task["_list_wellknown"] = str(todo_list.get("wellknownListName") or "")
                    changed.append(task)
    finally:
        if not http:
            await client.aclose()
    changed.sort(key=lambda t: (_iso_instant(t.get("lastModifiedDateTime")), t.get("id", "")))
    page = _oldest_page(changed, lambda t: _iso_instant(t.get("lastModifiedDateTime")), _TODO_PAGE)
    _log_page_cut(wf, "Microsoft To Do", page)
    await _note_backlog(redis, wf, "Microsoft To Do", page.more)
    if page.cursor is not None:
        await redis.set(cursor_key, page.cursor.get("lastModifiedDateTime", ""), ex=_CURSOR_TTL_SECONDS)
    signals = bool(filter_config)
    payloads: list[dict] = []
    for task in page.items:
        # The state is kept for every change, the agent's own included.
        kind = await todo_change_kind(redis, wf.id, task)
        if await is_own_todo_change(redis, wf, task):
            continue
        payloads.append(
            todo_task_payload(
                task, list_id=task["_list_id"], list_name=task["_list_name"], change_kind=kind, signals=signals
            )
        )
    return payloads


async def _poll_microsoft_flag_changes(
    token: str,
    wf: Assignment,
    redis: Redis,
    http: httpx.AsyncClient | None = None,
    *,
    integration_id: str | None = None,
) -> list[dict]:
    """Poll the mail whose follow-up flag changed since the last check.

    Reads the mail modified after the cursor (``lastModifiedDateTime``, oldest
    first, in the trigger's ``folder`` else every folder) and keeps those whose
    flag status changed (``flag_changes``). The first poll starts from now.
    """
    cursor_key = f"event_cursor:{wf.id}"
    last_seen_dt = await redis.get(cursor_key)
    if not last_seen_dt:
        return await _seed_from_now(redis, cursor_key)
    base, select = _mail_listing(wf)
    client = http or egress_client()
    try:
        resp = await client.get(
            base,
            headers={"Authorization": f"Bearer {token}"},
            params={
                "$top": str(_FLAG_PAGE + 1),
                "$orderby": "lastModifiedDateTime asc",
                "$filter": f"lastModifiedDateTime gt {last_seen_dt}",
                "$select": select + ",lastModifiedDateTime",
            },
            timeout=30,
        )
    finally:
        if not http:
            await client.aclose()
    if resp.status_code != 200:
        logger.warning(
            "Microsoft Graph messages returned %d for the flags of assignment %s – %s",
            resp.status_code,
            wf.id,
            resp.text[:300],
        )
        return []
    modified = [m for m in resp.json().get("value", []) if isinstance(m, dict)]
    modified.sort(key=lambda m: (_iso_instant(m.get("lastModifiedDateTime")), m.get("id", "")))
    page = _oldest_page(modified, lambda m: _iso_instant(m.get("lastModifiedDateTime")), _FLAG_PAGE)
    _log_page_cut(wf, "Microsoft mail flags", page)
    await _note_backlog(redis, wf, "Microsoft mail flags", page.more)
    if page.cursor is not None:
        await redis.set(cursor_key, page.cursor.get("lastModifiedDateTime", ""), ex=_CURSOR_TTL_SECONDS)
    signals = bool(trigger_filter(getattr(wf, "trigger_config", None)))
    return [
        flag_change_payload(msg, previous, signals=signals)
        for msg, previous in await flag_changes(redis, wf.id, page.items)
        if not await is_agent_mail(redis, integration_id, msg, wf.id)
    ]


async def mailbox_addresses(
    redis: Redis, http: httpx.AsyncClient | None, provider: str, token: str, integration_id: str
) -> set[str]:
    """The mailbox owner's addresses, for a ``to_me`` / ``cc_me`` filter (cached a day).

    Empty when they cannot be read: the filter then lets every mail through.
    """
    key = f"event_mailbox:{integration_id}"
    try:
        cached = await redis.get(key)
        if cached:
            return {str(a).lower() for a in json.loads(cached) if a}
    except Exception as exc:
        logger.debug("Could not read the mailbox addresses of integration %s: %r", integration_id, exc)
    client = http or egress_client()
    addresses: set[str] = set()
    try:
        if provider == "microsoft":
            resp = await client.get(
                f"{GRAPH_API}/me",
                headers={"Authorization": f"Bearer {token}"},
                params={"$select": "mail,userPrincipalName,proxyAddresses"},
                timeout=20,
            )
            if resp.status_code == 200:
                data = resp.json()
                addresses = {a for a in (data.get("mail"), data.get("userPrincipalName")) if isinstance(a, str)}
                addresses |= {
                    proxy.split(":", 1)[1]
                    for proxy in data.get("proxyAddresses") or []
                    if isinstance(proxy, str) and proxy.lower().startswith("smtp:")
                }
        elif provider == "google":
            resp = await client.get(
                f"{GMAIL_API}/users/me/profile", headers={"Authorization": f"Bearer {token}"}, timeout=20
            )
            if resp.status_code == 200 and isinstance(resp.json().get("emailAddress"), str):
                addresses = {resp.json()["emailAddress"]}
    except Exception as exc:
        logger.warning("Could not read the mailbox addresses of integration %s: %r", integration_id, exc)
    finally:
        if not http:
            await client.aclose()
    addresses = {a.strip().lower() for a in addresses if a.strip()}
    if addresses:
        try:
            await redis.set(key, json.dumps(sorted(addresses)), ex=_MAILBOX_TTL_SECONDS)
        except Exception as exc:
            logger.debug("Could not keep the mailbox addresses of integration %s: %r", integration_id, exc)
    return addresses


async def advance_event_cursor(redis: Redis, assignment_id: object, instant: object) -> None:
    """Move a Graph poll cursor up to *instant*, never back (a notified event).

    While a subscription delivers, the poller skips the assignment; a lapse
    then polls from the last event notified rather than from when the
    subscription started, and the seen set skips what already ran.
    """
    if not isinstance(instant, str) or not instant:
        return
    key = f"event_cursor:{assignment_id}"
    try:
        current = await redis.get(key)
        if isinstance(current, bytes):
            current = current.decode()
        if current and _iso_instant(current) >= _iso_instant(instant):
            return
        await redis.set(key, instant, ex=_CURSOR_TTL_SECONDS)
    except Exception as exc:
        logger.debug("Could not advance the event cursor of assignment %s: %r", assignment_id, exc)


async def seed_event_cursor(redis: Redis, assignment_id: object) -> None:
    """Start a cursor from now unless one exists, so polling resumed after a
    subscription reads on from it instead of replaying the latest page."""
    try:
        await redis.set(
            f"event_cursor:{assignment_id}",
            datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ex=_CURSOR_TTL_SECONDS,
            nx=True,
        )
    except Exception as exc:
        logger.debug("Could not seed the event cursor of assignment %s: %r", assignment_id, exc)


async def _poll_slack_messages(
    token: str,
    channels: list[str],
    wf: Assignment,
    redis: Redis,
    http: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Poll Slack for new channel messages via ``conversations.history``.

    Channel IDs come from the element picker (Slack channel IDs like
    ``C01234567``).  When no channels are configured, discovers channels
    via ``conversations.list`` (capped at ``_MAX_DISCOVERED_CHANNELS``).
    """

    cursor_key = f"event_cursor:{wf.id}"
    last_seen_ts = await redis.get(cursor_key)

    client = http or egress_client()
    headers = {"Authorization": f"Bearer {token}"}

    try:
        channel_targets: list[tuple[str, str]] = []  # (channel_id, label)

        if channels:
            for ch in channels:
                channel_targets.append((ch, ch))
        else:
            resp = await client.get(
                "https://slack.com/api/conversations.list",
                headers=headers,
                params={
                    "types": "public_channel,private_channel",
                    "exclude_archived": "true",
                    "limit": str(_MAX_DISCOVERED_CHANNELS),
                },
                timeout=20,
            )
            if resp.status_code != 200:
                logger.warning(
                    "Slack conversations.list returned %d for assignment %s",
                    resp.status_code,
                    wf.id,
                )
                return []
            data = resp.json()
            if not data.get("ok"):
                logger.warning(
                    "Slack conversations.list error for assignment %s: %s",
                    wf.id,
                    data.get("error", "unknown"),
                )
                return []
            for ch in data.get("channels", []):
                channel_targets.append((ch["id"], ch.get("name", ch["id"])))

        if not channel_targets:
            if not last_seen_ts:
                await redis.set(
                    cursor_key,
                    str(datetime.now(UTC).timestamp()),
                    ex=_CURSOR_TTL_SECONDS,
                )
            return []

        # Slack lists the latest first: after the cursor, each channel is read back
        # to it, and the oldest page across the channels is dispatched. A
        # first poll reads the latest page.
        fetched: list[tuple[str, str, dict]] = []
        for channel_id, label in channel_targets:
            params: dict[str, str] = {
                "channel": channel_id,
                "limit": str(_SLACK_LIST_PAGE if last_seen_ts else _CHANNEL_MESSAGE_PAGE),
            }
            if last_seen_ts:
                params["oldest"] = last_seen_ts

            pages = read = 0
            while True:
                resp = await client.get(
                    "https://slack.com/api/conversations.history",
                    headers=headers,
                    params=params,
                    timeout=30,
                )
                data = resp.json() if resp.status_code == 200 else {}
                if not data.get("ok"):
                    logger.debug(
                        "Slack history for %s returned error: %s",
                        label,
                        data.get("error") or resp.status_code,
                    )
                    if pages:
                        _log_unfetched(wf, f"Slack channel {label}", "could not be read back to its cursor")
                    break
                pages += 1
                messages = data.get("messages", [])
                fetched.extend((channel_id, label, msg) for msg in messages)
                read += len(messages)
                if not last_seen_ts or not data.get("has_more"):
                    break
                next_cursor = (data.get("response_metadata") or {}).get("next_cursor")
                if not next_cursor or pages >= _SLACK_MAX_LIST_PAGES:
                    _log_unfetched(wf, f"Slack channel {label}", f"has more than the {read} new messages a poll reads")
                    break
                params = {**params, "cursor": next_cursor}

        fetched.sort(key=lambda item: (_slack_ts(item[2].get("ts", "")), item[0]))
        cursor_ts = ""
        if last_seen_ts:
            page = _oldest_page(fetched, lambda item: _slack_ts(item[2].get("ts", "")), _CHANNEL_MESSAGE_PAGE)
            fetched = page.items
            _log_page_cut(wf, "Slack", page)
            await _note_backlog(redis, wf, "Slack", page.more)
            if page.cursor is not None:
                cursor_ts = page.cursor[2].get("ts", "")
        elif fetched:
            cursor_ts = fetched[-1][2].get("ts", "")

        events: list[dict] = []
        for channel_id, label, msg in fetched:
            if slack_message_skipped(msg):
                continue
            if await is_agent_slack_post(redis, channel_id, msg.get("ts", ""), wf.id):
                continue
            events.append(
                {
                    "text": msg.get("text", ""),
                    "from_name": msg.get("user", ""),
                    "channel_name": label,
                    "channel_id": channel_id,
                    "created_at": msg.get("ts", ""),
                    "message_id": slack_message_key(channel_id, msg.get("ts", "")),
                    "trigger_type": "integration_event",
                    "event": "new_slack_message",
                    "provider": "slack",
                    # One turn per conversation, as the events endpoint stamps it.
                    CONVERSATION_KEY_FIELD: slack_conversation_key(
                        channel_id, msg.get("ts", ""), msg.get("thread_ts")
                    ),
                }
            )

        # The cursor moves past every message read, a skipped one included.
        if cursor_ts:
            await redis.set(cursor_key, cursor_ts, ex=_CURSOR_TTL_SECONDS)
        elif not last_seen_ts:
            await redis.set(
                cursor_key,
                str(datetime.now(UTC).timestamp()),
                ex=_CURSOR_TTL_SECONDS,
            )

        # Dispatched oldest first, across channels.
        return events
    finally:
        if not http:
            await client.aclose()


async def _gmail_received_dates(
    client: httpx.AsyncClient, token: str, wf: Assignment, redis: Redis, message_ids: list[str]
) -> dict[str, int]:
    """When Gmail received each of *message_ids* (epoch ms; 0 when it does not say).

    The listing does not say, and a backlog is listed again on every poll until it
    drains: each message is asked once, the answers kept per assignment.
    """
    key = f"event_gmail_dates:{wf.id}"
    dates: dict[str, int] = {}
    try:
        for msg_id, value in zip(message_ids, await redis.hmget(key, message_ids), strict=True):
            if value is not None:
                dates[msg_id] = int(value)
    except Exception as exc:
        logger.debug("Could not read the Gmail dates kept for assignment %s: %r", wf.id, exc)

    gate = asyncio.Semaphore(_GMAIL_DATE_CONCURRENCY)

    async def received(msg_id: str) -> tuple[str, int]:
        async with gate:
            resp = await client.get(
                f"{GMAIL_API}/users/me/messages/{msg_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"format": "minimal", "fields": "internalDate"},
                timeout=30,
            )
        return msg_id, _gmail_internal_date(resp.json()) if resp.status_code == 200 else 0

    missing = [msg_id for msg_id in message_ids if msg_id not in dates]
    dates.update(await asyncio.gather(*(received(msg_id) for msg_id in missing)))

    known = {msg_id: str(date) for msg_id, date in dates.items() if date}
    try:
        await redis.delete(key)
        if known:
            await redis.hset(key, mapping=known)
            await redis.expire(key, _CURSOR_TTL_SECONDS)
    except Exception as exc:
        logger.debug("Could not keep the Gmail dates of assignment %s: %r", wf.id, exc)
    return dates


async def _poll_google_emails(
    token: str, wf: Assignment, redis: Redis, http: httpx.AsyncClient | None = None
) -> list[dict]:
    """Poll Gmail API for new emails."""

    cursor_key = f"event_cursor:{wf.id}"
    last_seen_ts = await redis.get(cursor_key)
    filter_config = trigger_filter(getattr(wf, "trigger_config", None))
    query = "is:unread"
    folder = filter_config.get("folder")
    if isinstance(folder, str) and folder.strip():
        query += f' label:"{folder.strip()}"'
    if last_seen_ts:
        query += f" after:{last_seen_ts}"
    metadata_headers = ["From", "To", "Cc", "Subject", "Date"]
    if filter_config:
        metadata_headers += [h for h in _GMAIL_FILTER_HEADERS if h not in metadata_headers]

    # Reuse the shared client when provided; otherwise create one for this call
    client = http or egress_client()
    # Set once the page is the oldest of a backlog: the cursor stops on it.
    backlog_cursor: int | None = None
    try:
        resp = await client.get(
            f"{GMAIL_API}/users/me/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": query, "maxResults": _GMAIL_LIST_MAX if last_seen_ts else _GMAIL_PAGE},
            timeout=30,
        )
        if resp.status_code != 200:
            logger.warning(
                "Gmail /messages returned %d for assignment %s – body: %s",
                resp.status_code,
                wf.id,
                resp.text[:500] or "(empty)",
            )
            return []
        data = resp.json()

        message_ids = [m["id"] for m in data.get("messages", [])]
        if not message_ids:
            if not last_seen_ts:
                await redis.set(
                    cursor_key, str(int(datetime.now(UTC).timestamp())), ex=_CURSOR_TTL_SECONDS
                )
            return []

        page_ids = message_ids[:_GMAIL_PAGE]
        if last_seen_ts:
            more = len(message_ids) > _GMAIL_PAGE or bool(data.get("nextPageToken"))
            await _note_backlog(redis, wf, "Gmail", more)
            if more:
                # The listing has no specified order: the oldest page is picked by
                # when Gmail received each message, the cursor on the second the page
                # ends (``after:`` reads whole seconds).
                if data.get("nextPageToken"):
                    _log_unfetched(wf, "Gmail", f"has more than {_GMAIL_LIST_MAX} unread messages after its cursor")
                received = await _gmail_received_dates(client, token, wf, redis, message_ids)
                ordered = sorted(message_ids, key=lambda msg_id: (received[msg_id], msg_id))
                page = _oldest_page(ordered, lambda msg_id: received[msg_id] // 1000, _GMAIL_PAGE)
                _log_page_cut(wf, "Gmail", page)
                page_ids = page.items
                # A cursor that did not move would list the same page again.
                backlog_cursor = max(received[page.cursor] // 1000, _int_or_zero(last_seen_ts) + 1)

        # The listing has no specified order: sorted by internalDate below.
        dated: list[tuple[int, str, dict]] = []
        for msg_id in page_ids:
            resp = await client.get(
                f"{GMAIL_API}/users/me/messages/{msg_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "format": "metadata",
                    "metadataHeaders": metadata_headers,
                },
                timeout=30,
            )
            if resp.status_code != 200:
                logger.warning(
                    "Gmail /messages/%s returned %d for assignment %s",
                    msg_id,
                    resp.status_code,
                    wf.id,
                )
                continue
            msg = resp.json()
            headers = {
                h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])
            }
            event = {
                "from": headers.get("From", ""),
                "to": _header_addresses(headers.get("To", "")),
                "cc": _header_addresses(headers.get("Cc", "")),
                "subject": headers.get("Subject", ""),
                "body": msg.get("snippet", ""),
                "received_at": headers.get("Date", ""),
                "message_id": msg_id,
                # The thread: the mail trigger's ordering key.
                "thread_id": msg.get("threadId", ""),
                "trigger_type": "integration_event",
                "event": "new_email",
                "provider": "google",
            }
            if filter_config:
                event[FILTER_SIGNALS_KEY] = {
                    "to": headers.get("To", ""),
                    "cc": headers.get("Cc", ""),
                    "headers": headers,
                }
            dated.append((_gmail_internal_date(msg), msg_id, event))
        # Dispatched oldest first.
        dated.sort(key=lambda item: (item[0], item[1]))
        events = [event for _date, _id, event in dated]
    finally:
        if not http:
            await client.aclose()

    # Past a backlog's page, or now when everything listed was read.
    cursor = backlog_cursor if backlog_cursor is not None else int(datetime.now(UTC).timestamp())
    await redis.set(cursor_key, str(cursor), ex=_CURSOR_TTL_SECONDS)

    return events
