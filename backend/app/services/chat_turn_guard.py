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

"""Single-flight guard for chat conversation turns.

A message sent to a conversation while a previous turn is still running
server-side must NEVER spawn a second concurrent agent loop: double token
burn, interleaved assistant messages, racing compaction.  A turn keeps
running when its stream drops (page reload, proxy kill, network blip,
#2314), so the next send may come from a client that never saw it.

This module makes the ``chat:processing:{conv_id}`` marker an enforced,
single-flight **turn lock**:

* :func:`acquire_turn` — ``SET NX`` a unique turn token on the marker
  key.  Exactly one turn per conversation can hold it; the holder's
  keepalive refreshes it (:func:`refresh_turn`) and every exit path
  releases it (:func:`release_turn`).
* :func:`fold_into_inflight_turn` — when acquisition fails, the new
  message is pushed onto a Redis **steering inbox** instead of starting
  a second loop.  The running turn drains the inbox into its in-process
  steering queue (same iteration-boundary injection as any other
  steering — see ``drain_steering_queue`` in ``agent_loop``).
* :func:`drain_steering_inbox` — consumed by the running turn's bridge
  task (every ``BRIDGE_POLL_SECONDS``) and by the fold-in leftover pass.
* :func:`release_turn_and_drain` — the turn-exit handshake: release the
  lock, then drain the inbox one last time so a fold that raced the
  release is either answered in a follow-up pass or handed back.

Lock **ownership** operations (release, owned-refresh) are atomic
single-key Lua compare-and-writes — a plain GET-then-DEL /
GET-then-write would race a TTL lapse + takeover and delete or clobber
the NEW holder's lock.  Single-key scripts are safe in every supported
Redis topology (standalone and Sentinel; SAQ already requires Lua).

The **fold handshake** needs no scripting, except the push of a
partner's answer: one Lua call that refuses it once a Stop at or after
its work's submission is recorded.  Each inbox entry is a JSON
payload with a unique id; the folding side runs a
push → check-marker → ``LREM``-recover handshake so every entry is
consumed exactly once even when the running turn releases the lock at
the same instant:

* marker still present after the push → the running turn WILL drain the
  entry (its exit path drains the inbox again after releasing).
* marker gone → try ``LREM`` of our exact payload: removed (1) means
  nobody consumed it and the caller must run its own turn; not found
  (0) means the exiting turn drained it and will answer in a follow-up
  pass.

Crash staleness is bounded by ``TURN_MARKER_TTL_SECONDS``: a process
that dies without releasing leaves the marker for at most one TTL, and
folded texts are ALWAYS persisted as regular user messages first, so a
stranded inbox entry is never lost — it is already part of the stored
thread and shapes the next turn's history.  For the same reason the
next acquisition PURGES the inbox: an entry still present when
a new turn takes the lock was never consumed by the turn it was folded
into (crashed marker, ``requeue_stranded``), and injecting it as
steering would deliver it a second time — ordered AFTER the new user
message — on top of its persisted copy in history.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from app.core import worker_shutdown
from app.core.redaction import redact_known_secrets_for_display
from app.core.utils import chat_processing_key

logger = logging.getLogger(__name__)


async def _run_shielded[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run *coro* to completion even when the awaiting task is cancelled.

    The lock-release operations below run inside teardown paths (REST
    ``finally`` blocks, the web chat's ``_clear_processing_marker`` cleanup) that
    can execute while the surrounding task is being CANCELLED (client
    disconnect, handler teardown).  An unshielded ``await`` there is
    interrupted at its first suspension point, skipping the release and
    stranding the turn lock for a full TTL — every send in that window
    would fold into a turn that no longer exists.

    Double-shield idiom: on
    cancellation, wait the inner task out before propagating so the
    release has completed by the time the ``CancelledError`` reaches the
    caller.  A second cancellation during that wait still propagates —
    the inner task then finishes in the background.
    """
    task = asyncio.ensure_future(coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if not task.done():
            with contextlib.suppress(Exception):
                await asyncio.shield(task)
        raise


async def _redis():
    """Resolve the shared Redis client lazily.

    Resolved at call time (not import time) so test fixtures that patch
    ``app.core.redis.redis_client`` transparently apply to this module.
    """
    from app.core.redis import redis_client

    return await redis_client()


@dataclass
class SteeringPayload:
    """A steering message folded into an in-flight turn.

    ``file_ids`` carries FileAttachment ids so a screenshot/document sent
    mid-run from another stream reaches the running loop as a multimodal
    steering message (the bridge resolves them to ``FileRef`` objects and
    puts them ON the steering user message, where every provider inlines
    them - that is the point of a screenshot).

    ``context_file_ids`` carries FileAttachment ids the bridge must only
    REGISTER in the running loop's FileContext, never inline: the
    files a background sub-agent task produced, handed over by a parent wake
    that folded into the turn already running. The wake text names them and
    the model reads one through ``read_file_content`` when it needs it; put
    on the message instead, nine CSVs would be extracted into ~720k
    characters of steering text before the model could choose to read one.

    ``partner_wake`` marks the answer of an A2A partner, folded by a partner
    wake. A wake turn the user stopped leaves such a fold for the
    next turn instead of following it up; a message a person sent, and a
    sub-agent task's wake, are followed up whatever the Stop.

    ``delegated_at`` is when the work a partner's answer delivers was sent.
    A Stop at or after it stopped that work: the fold is refused
    once such a Stop is recorded, and a drain that finds one pushed before
    the Stop does not deliver it. ``None`` - an entry from a replica that
    predates it - reads as before.

    ``mentioned_agent_ids`` are the agents the folded message @mentions
    besides the one already answering, in mention order: the turn
    that consumes the entry answers the message with them once its own
    agents have. Nothing else carries them - the message is persisted, its
    mentions are not - so they survive :func:`requeue_stranded` and are
    handed to the turn whose acquisition purges the entry
    (:func:`acquire_or_fold`'s ``purged_mentions``).

    ``mentions_only`` marks an entry handed back by a turn that ended before
    it answered those agents: the text already reached the thread, so the
    turn that drains it answers them and never injects the text again.

    ``platform_text`` marks a text the platform wrote (a parent wake, a
    partner's answer as the wake presents it), never what a person sent: a
    follow-up turn restores the secret variable markers of such a text only,
    and defuses a person's like any message they write.

    ``message_id`` is the stored user message a web steer was persisted as.
    A steer the loop never took (sent while the final answer streamed) is
    answered by a follow-up pass: the turn moves that message after the
    answer it did not shape, so the thread never reads the steer above it.
    """

    text: str
    file_ids: list[str] = field(default_factory=list)
    context_file_ids: list[str] = field(default_factory=list)
    partner_wake: bool = False
    delegated_at: datetime | None = None
    mentioned_agent_ids: list[str] = field(default_factory=list)
    mentions_only: bool = False
    platform_text: bool = False
    message_id: str | None = None


# TTL of the turn marker.  The holder refreshes it every ~20s (the web
# chat's refresher) / ``REST_REFRESH_INTERVAL_SECONDS`` (REST refresher), so a
# live turn never expires; a crashed process leaves it for at most one
# TTL.  Matches the historical ``chat:processing`` TTL.
TURN_MARKER_TTL_SECONDS = 300

# TTL of the steering inbox list.  Longer than the marker TTL so a text
# folded moments before a crash survives until the next turn's start
# drain; entries are also persisted user messages, so expiry only costs
# the (already-degraded) immediate injection.
STEERING_INBOX_TTL_SECONDS = 600

# How long a pause may go unattended — no client connected and no decision —
# before the turn gives up. Not an approval deadline: it only starts counting
# once the reviewer's client is gone, and any reconnection resets it. A person
# staring at a prompt is never on a clock; a turn nobody can reach any more
# should not wait forever for them.
ABANDONED_APPROVAL_SECONDS = 1800

#: How long one contact from a client counts as "somebody is there".
#:
#: Deliberately far below ``ABANDONED_APPROVAL_SECONDS``, and that is the whole
#: point: this mark suppresses the abandonment count while it lives, so setting
#: it to the window itself made the two add up — a reload at T=0 kept the mark
#: to T=1800, the count then needed another 1800, and the turn held the
#: conversation for ~60 minutes against a documented 30. A short mark means the
#: measurement is done by the count, where it is legible, rather than half by a
#: TTL nobody reads.
CLIENT_SEEN_TTL_SECONDS = 60


class ApprovalAbandonedError(Exception):
    """No client has been connected long enough that nobody can answer."""


def client_seen_key(conversation_id: str) -> str:
    """Redis key stamped whenever a client shows itself for a conversation."""
    return f"chat:approval:seen:{conversation_id}"


async def mark_client_seen(
    conversation_id: str, ttl: int = CLIENT_SEEN_TTL_SECONDS
) -> None:
    """Record that a client is there, for the abandonment check to find.

    Liveness cannot be judged from the stream alone. A reload is a *new*
    request: the old stream is gone for good and its "client present" flag stays
    false forever, while the person is back and looking at the recovered prompt.
    Anything conversation-scoped survives that; a request-scoped flag cannot.

    The TTL is short on purpose (see ``CLIENT_SEEN_TTL_SECONDS``): this mark
    suppresses the abandonment count while it lives, so a window-length TTL made
    the two add up to twice the documented limit. Every fresh contact restarts
    it, and the count does the measuring.
    """
    r = await _redis()
    await r.set(client_seen_key(conversation_id), "1", ex=ttl)


# How often the running turn's bridge task polls the inbox for texts
# folded from other streams / processes.
BRIDGE_POLL_SECONDS = 1.0

# How long a turn's end waits for its bridge to queue the batch it holds.
BRIDGE_STOP_SECONDS = 5.0


async def stop_bridge(task: asyncio.Task | None, stop: asyncio.Event) -> None:
    """Stop a turn's steering bridge once the batch it took off the inbox is queued.

    Cancelled mid-batch, a bridge loses what it drained: the inbox no longer
    holds it and its queue never got it, so nothing answers it. The turn's end
    sets *stop*, which the bridge checks between polls, and waits for it to
    return, for at most ``BRIDGE_STOP_SECONDS``; one still running then is
    cancelled. A cancellation of the caller cancels the bridge too.
    """
    if task is None:
        return
    stop.set()
    try:
        await asyncio.wait_for(asyncio.shield(task), BRIDGE_STOP_SECONDS)
    except TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    except asyncio.CancelledError:
        if not task.cancelled():
            task.cancel()
            raise
    except Exception:
        logger.debug("A steering bridge failed", exc_info=True)


# Refresh cadence for REST turns.
REST_REFRESH_INTERVAL_SECONDS = 60.0

# Bounded attempts of the acquire → fold handshake in
# :func:`acquire_or_fold`.  An acquire failure means another turn holds
# the lock; a fold failure means that turn finished mid-hand-off.
# Alternating failures require a new turn starting AND finishing inside
# every hand-off window — three rounds make the requeue fallback
# practically unreachable.
ACQUIRE_FOLD_ATTEMPTS = 3


def processing_key(conversation_id: str) -> str:
    """Redis key of the per-conversation turn marker / lock.

    Delegates to :func:`app.core.utils.chat_processing_key` — the single
    source of truth for the marker's key format, shared with the
    ``is_processing`` probes and the stale-run sweep's agent_chat
    liveness probe.  A silent format drift here would make the
    sweep misclassify live chat turns as orphaned runs.
    """
    return chat_processing_key(conversation_id)


def steering_inbox_key(conversation_id: str) -> str:
    """Redis key of the per-conversation steering inbox."""
    return f"chat:steering:{conversation_id}"


async def list_processing_conversations() -> list[str]:
    """Ids of every conversation with an in-flight turn marker, cluster-wide.

    The emergency stop (``services/panic.py``) has to reach turns running on
    other replicas, and the marker is the only cross-process record of them.
    ``SCAN`` rather than ``KEYS`` so a large deployment does not stall Redis;
    the prefix is the one :func:`processing_key` builds, so a format drift
    there would empty this list rather than mis-target a cancel.
    """
    r = await _redis()
    prefix = processing_key("")
    ids: list[str] = []
    async for raw_key in r.scan_iter(match=f"{prefix}*", count=500):
        key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
        conversation_id = key[len(prefix) :]
        if conversation_id:
            ids.append(conversation_id)
    return ids


def approval_inbox_key(conversation_id: str) -> str:
    """Redis key of the per-conversation approval-decision inbox."""
    return f"chat:approval:{conversation_id}"


def pending_approvals_key(conversation_id: str) -> str:
    """Redis key holding the proposals a paused turn is still waiting on."""
    return f"chat:approval:pending:{conversation_id}"


async def set_pending_approvals(conversation_id: str, proposals: list) -> None:
    """Record what a turn paused for, so a reconnecting client can be re-asked.

    The ``approval_required`` frame is sent once, over the turn's streams. Without this
    the proposals exist nowhere else: a reload, a closed tab or a dropped
    connection loses the prompt while :func:`await_approval_decision` goes on
    waiting forever, and because the turn keeps the processing marker alive the
    whole conversation stops answering with nothing on screen to explain it.

    Redis rather than the database deliberately. A paused turn is process-bound
    — the coroutine waiting on the inbox dies with its worker — so surviving a
    restart would only strand proposals no turn is listening for. What has to
    survive is the *stream*, and Redis covers that.

    TTL matches the turn marker and is refreshed with it (:func:`refresh_turn`),
    so the record lives exactly as long as the pause it describes.
    """
    r = await _redis()
    # Drop anything left in the inbox first. A decision that arrived after its
    # turn stopped waiting — a client retry, a double submit — outlives it under
    # the inbox TTL, and the next pause would pop that stale payload
    # immediately. Its ids match nothing, every proposal defaults to a refusal,
    # and the batch is declined on behalf of a reviewer who never saw it.
    await r.delete(approval_inbox_key(conversation_id))
    await r.set(
        pending_approvals_key(conversation_id),
        # The copy a reloaded page and the conversation endpoints show:
        # a secret variable value as the display text. The turn waiting on the
        # decision runs the proposals it holds in memory, never this copy.
        json.dumps(redact_known_secrets_for_display(proposals)),
        ex=TURN_MARKER_TTL_SECONDS,
    )


async def get_pending_approvals(conversation_id: str) -> list | None:
    """Proposals a turn is still waiting on, or ``None`` when it is not paused."""
    r = await _redis()
    raw = await r.get(pending_approvals_key(conversation_id))
    if raw is None:
        return None
    try:
        proposals = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Discarding unparseable pending approvals for %s", conversation_id
        )
        return None
    return proposals if isinstance(proposals, list) else None


async def clear_pending_approvals(conversation_id: str) -> None:
    """Forget the pause — the decision arrived, or the turn ended without one."""
    r = await _redis()
    await r.delete(pending_approvals_key(conversation_id))
    await r.delete(approval_inbox_key(conversation_id))


async def push_approval_decision(conversation_id: str, decisions: dict) -> bool:
    """Hand a reviewer's decisions to the conversation's waiting turn.

    Returns ``False`` when no turn is currently waiting — the turn finished, was
    cancelled, or the decision arrived twice. The caller reports that rather than
    leaving the decision to rot in Redis, because a client that thinks it
    approved something and sees nothing happen has no way to tell why.

    Redis rather than an in-process queue, matching steering: the API request
    carrying the decision can land on a different replica from the one running
    the turn.
    """
    r = await _redis()
    # Either signal will do. The turn marker is held by the chat transports;
    # the embedded-assistant path runs a turn without one, so requiring the
    # marker alone refused every decision it ever sent and left its paused turn
    # waiting for an answer the API had already rejected. A recorded pause is
    # the more direct evidence anyway: it exists exactly while a turn is
    # blocked on this inbox.
    if not (
        await r.exists(processing_key(conversation_id))
        or await r.exists(pending_approvals_key(conversation_id))
    ):
        return False
    inbox = approval_inbox_key(conversation_id)
    await r.rpush(inbox, json.dumps(decisions))
    await r.expire(inbox, STEERING_INBOX_TTL_SECONDS)
    return True


async def await_approval_decision(
    conversation_id: str,
    *,
    poll_seconds: float = 0.5,
    client_present: Callable[[], bool] | None = None,
    abandon_after: float = ABANDONED_APPROVAL_SECONDS,
) -> dict:
    """Block until a decision arrives for this conversation.

    Deliberately unbounded. A reviewer may be reading a proposed argument list
    carefully, stepping away, or asking a colleague — and there is no safe
    default action to take on their behalf. Auto-approving would defeat the
    gate; auto-rejecting would punish someone for thinking. The turn's own
    cancellation path is what ends the wait.

    Unbounded here is only true if the caller makes it true: a chat turn runs
    under a wall-clock budget, so the pause is measured and both that budget and
    the loop's deadline are moved by however long the reviewer took. The budget
    bounds work, not people.

    ``client_present`` lets a caller that can tell whether anyone is still
    connected bound the *unattended* case — a stream that dropped and never came
    back. That is not a deadline on the reviewer: the count only runs while no
    client is there, and any reconnection resets it. Callers that cannot tell
    pass nothing and wait as before.

    Polls rather than blocking on Redis so the surrounding turn keeps its
    cancellation checks responsive.
    """
    r = await _redis()
    inbox = approval_inbox_key(conversation_id)
    pending = pending_approvals_key(conversation_id)
    unattended = 0.0
    while True:
        # This coroutine *is* the proof the turn is still waiting, so it keeps
        # the record alive rather than leaving it to a TTL. The chat transports
        # refresh it from their turn marker; the embedded-assistant path holds
        # no marker, so without this the record expired after one TTL and the
        # decision endpoint began answering 409 to a reviewer who was still
        # looking at the prompt — a deadline nobody chose and nobody could see,
        # which is exactly what the contract promises not to impose.
        await r.expire(pending, TURN_MARKER_TTL_SECONDS)
        item = await r.lpop(inbox)
        if item is not None:
            try:
                return json.loads(item)
            except (TypeError, ValueError):
                logger.warning(
                    "Discarding unparseable approval decision for %s",
                    conversation_id,
                )
                continue
        await asyncio.sleep(poll_seconds)

        if (client_present is None or client_present()) or await r.exists(
            client_seen_key(conversation_id)
        ):
            # Someone is there — on this stream, or by having shown themselves
            # since. The second half is what makes a reload survivable: the
            # stream that carried the prompt is gone and its flag will never say
            # otherwise, so presence has to be read from the conversation rather
            # than from the request that happens to be waiting.
            unattended = 0.0
            continue
        unattended += poll_seconds
        if unattended >= abandon_after:
            logger.warning(
                "Approval for %s abandoned after %.0fs with no client",
                conversation_id,
                unattended,
            )
            await clear_pending_approvals(conversation_id)
            raise ApprovalAbandonedError(conversation_id)


# ── Page actions ─────────────────────────────────────────────────
# ``act_on_page`` sends one action at a time to the embedded widget and waits
# for the page's answer, which comes back on the approval route and may land on
# another replica. Kept apart from the approval keys: the pending-approvals
# read would otherwise hand a page action to the widget's approval prompt.

PAGE_ACTION_TTL_SECONDS = 60


def page_action_pending_key(conversation_id: str) -> str:
    """Redis key holding the id of the page action a turn is waiting on."""
    return f"chat:page_action:pending:{conversation_id}"


def page_action_inbox_key(conversation_id: str) -> str:
    """Redis key of the per-conversation page-action answer inbox."""
    return f"chat:page_action:{conversation_id}"


async def set_pending_page_action(conversation_id: str, action_id: str) -> None:
    """Record the action the turn is about to send, before the frame goes out."""
    r = await _redis()
    await r.delete(page_action_inbox_key(conversation_id))
    await r.set(
        page_action_pending_key(conversation_id), action_id, ex=PAGE_ACTION_TTL_SECONDS
    )


async def clear_pending_page_action(conversation_id: str) -> None:
    """Forget the wait - the answer arrived, timed out, or the run ended."""
    r = await _redis()
    await r.delete(page_action_pending_key(conversation_id))
    await r.delete(page_action_inbox_key(conversation_id))


async def push_page_action_result(conversation_id: str, result: dict) -> bool:
    """Hand the page's answer to the waiting turn; ``False`` when none waits for it.

    Delivered only when ``result["action_id"]`` is the action pending now. An
    answer to an earlier step - a slow page, a retried POST - would otherwise be
    read as the answer to the current one, and the next decision would run on a
    snapshot taken before the action it is judging.
    """
    r = await _redis()
    pending = await r.get(page_action_pending_key(conversation_id))
    if isinstance(pending, bytes):
        pending = pending.decode()
    if pending is None or pending != result.get("action_id"):
        return False
    inbox = page_action_inbox_key(conversation_id)
    await r.rpush(inbox, json.dumps(result))
    await r.expire(inbox, PAGE_ACTION_TTL_SECONDS)
    return True


async def await_page_action_result(
    conversation_id: str,
    action_id: str,
    *,
    timeout: float,
    block_seconds: float = 1.0,
) -> dict | None:
    """The page's answer to *action_id*, or ``None`` once *timeout* has passed.

    Blocks on the inbox rather than polling it, so the answer is read as soon
    as it is pushed; the pending key is kept alive once per block.
    """
    r = await _redis()
    inbox = page_action_inbox_key(conversation_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while (remaining := deadline - loop.time()) > 0:
        await r.expire(page_action_pending_key(conversation_id), PAGE_ACTION_TTL_SECONDS)
        popped = await r.blpop([inbox], timeout=min(block_seconds, remaining))
        if popped is None:
            continue
        try:
            result = json.loads(popped[1])
        except (TypeError, ValueError):
            logger.warning("Discarding unparseable page action answer for %s", conversation_id)
            continue
        if isinstance(result, dict) and result.get("action_id") == action_id:
            return result
    return None


def page_action_run_key(conversation_id: str) -> str:
    """Redis key held by the one ``act_on_page`` run of a conversation."""
    return f"chat:page_action:run:{conversation_id}"


# Deletes the run key only when it still holds the claimer's token: a run that
# outlived its TTL must not release the claim of the run that took over.
_RELEASE_RUN = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


async def claim_page_action_run(conversation_id: str, token: str, *, ttl_seconds: int) -> bool:
    """Claim the conversation's page for one run; ``False`` while another holds it.

    One page, one pending action: two runs on the same conversation would
    replace each other's pending id and drive the page at once. Held in Redis
    so the claim holds across replicas.
    """
    r = await _redis()
    return bool(await r.set(page_action_run_key(conversation_id), token, nx=True, ex=ttl_seconds))


async def release_page_action_run(conversation_id: str, token: str) -> None:
    """Release a claim taken by :func:`claim_page_action_run` with *token*."""
    r = await _redis()
    await r.eval(_RELEASE_RUN, 1, page_action_run_key(conversation_id), token)


#: Prefix of the token a turn running for a Slack / Teams message holds.
#: Such a turn has no steering bridge and answers on its channel, so a
#: web send must not fold into it: :func:`channel_turn_holder` names it.
CHANNEL_TURN_MARKER_PREFIX = "channel:"


def channel_turn_token_channel(token: object) -> str | None:
    """The channel a turn token says its turn runs for, or ``None``."""
    if isinstance(token, bytes):
        token = token.decode(errors="replace")
    if not isinstance(token, str) or not token.startswith(CHANNEL_TURN_MARKER_PREFIX):
        return None
    channel = token[len(CHANNEL_TURN_MARKER_PREFIX) :].split(":", 1)[0]
    return channel or None


#: How long a web send or a parent wake holds channel turns off on its way in,
#: between its check of the conversation and the moment it has its
#: turn or its fold. Released as soon as it has one, and renewed meanwhile
#: (:func:`renew_turn_entry`): the TTL only frees the reservation of a caller
#: that died on the way.
TURN_ENTRY_RESERVATION_SECONDS = 15

#: The longest a reservation is renewed: a caller that never released it (an
#: exception on the way) stops holding channel turns off after this.
TURN_ENTRY_RESERVATION_MAX_SECONDS = 120


def turn_entries_key(conversation_id: str) -> str:
    """Redis key of the reservations holding channel turns off."""
    return f"chat:turn_entries:{conversation_id}"


# Reserve unless a channel turn holds the lock, in one script: no channel
# turn can take the lock between the check and the reservation. Each
# reservation is a member scored by the moment it lapses, on the Redis clock.
_RESERVE_TURN_ENTRY_LUA = """
local holder = redis.call('GET', KEYS[1])
if holder and string.sub(holder, 1, #ARGV[1]) == ARGV[1] then
    return holder
end
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
redis.call('ZADD', KEYS[2], now + tonumber(ARGV[3]), ARGV[2])
redis.call('PEXPIRE', KEYS[2], ARGV[3])
return false
"""

# A channel turn takes the lock only while no reservation is live.
_ACQUIRE_CHANNEL_TURN_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
if redis.call('ZCARD', KEYS[2]) > 0 then
    return false
end
return redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2])
"""


async def reserve_turn_entry(conversation_id: str) -> tuple[str | None, str | None]:
    """Hold channel turns off while a web send or a wake joins the conversation.

    A turn run for a channel message has no steering bridge: what
    folds into it is only answered by the next turn. So a caller about to
    store a message and take or fold into the conversation's turn reserves
    its way in first. Returns ``(reservation, None)``: until
    :func:`release_turn_entry` (or ``TURN_ENTRY_RESERVATION_SECONDS``), a
    channel message waits instead of taking the lock, so the caller either
    takes the turn or folds into one that drains it. Returns
    ``(None, channel)`` when a channel message's turn already holds the
    lock: nothing is reserved.
    """
    reservation = uuid.uuid4().hex
    r = await _redis()
    holder = await r.eval(
        _RESERVE_TURN_ENTRY_LUA,
        2,
        processing_key(conversation_id),
        turn_entries_key(conversation_id),
        CHANNEL_TURN_MARKER_PREFIX,
        reservation,
        int(TURN_ENTRY_RESERVATION_SECONDS * 1000),
    )
    channel = channel_turn_token_channel(holder) if holder else None
    if channel:
        return None, channel
    return reservation, None


# Push a live reservation's lapse back by a full TTL; one that lapsed already
# stays gone.
_RENEW_TURN_ENTRY_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local lapses = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not lapses or tonumber(lapses) <= now then
    return 0
end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 1
"""


async def renew_turn_entry(conversation_id: str, reservation: str) -> bool:
    """Keep a :func:`reserve_turn_entry` reservation alive; ``False`` once it lapsed."""
    r = await _redis()
    return bool(
        await r.eval(
            _RENEW_TURN_ENTRY_LUA,
            1,
            turn_entries_key(conversation_id),
            reservation,
            int(TURN_ENTRY_RESERVATION_SECONDS * 1000),
        )
    )


async def release_turn_entry(conversation_id: str, reservation: str) -> None:
    """Give back a :func:`reserve_turn_entry` reservation (shielded)."""

    async def _inner() -> None:
        r = await _redis()
        await r.zrem(turn_entries_key(conversation_id), reservation)

    await _run_shielded(_inner())


async def acquire_turn(conversation_id: str, *, channel: str | None = None) -> str | None:
    """Try to become the conversation's single in-flight turn.

    Returns a unique turn token on success (the caller must pass it to
    :func:`refresh_turn` / :func:`release_turn`), or ``None`` when
    another turn already holds the lock. A turn run for a channel message
    passes its *channel*: the token then says so, and the lock is not taken
    while a web send or a wake holds a :func:`reserve_turn_entry`
    reservation. Any other turn is recorded until it ends
    (:func:`interrupted_turn`). Every turn counts among the work
    running in this process until its release (``worker_shutdown``).
    """
    token = uuid.uuid4().hex
    r = await _redis()
    if channel:
        token = f"{CHANNEL_TURN_MARKER_PREFIX}{channel}:{token}"
        ok = await r.eval(
            _ACQUIRE_CHANNEL_TURN_LUA,
            2,
            processing_key(conversation_id),
            turn_entries_key(conversation_id),
            token,
            TURN_MARKER_TTL_SECONDS,
        )
        if not ok:
            return None
        worker_shutdown.turn_started(conversation_id, token)
        return token
    ok = await r.set(
        processing_key(conversation_id),
        token,
        nx=True,
        ex=TURN_MARKER_TTL_SECONDS,
    )
    if not ok:
        return None
    await _record_open_turn(r, conversation_id, token)
    worker_shutdown.turn_started(conversation_id, token)
    return token


# ── A turn's end, owed to its conversation ───────────────────
# A web turn that takes the lock records itself; its release forgets the
# record once the turn reached its own end. A turn closed with the interrupted
# terminal (``TurnPublisher.aclose``: a shutdown, a persistence failure) marks
# it instead, and a process that dies leaves it as it is. A record whose turn
# no longer holds the lock is a turn that ended without its answer: the
# conversation says so (``GET /chat/conversations/{id}``), after a reload, a
# restart or on another device, until the next turn takes the lock or the
# person dismisses it. Plain commands, no script: only the lock holder writes
# the record, and it settles the record before it gives the lock up.
# Best-effort: a record that cannot be written costs the notice, never the turn.

#: How long the record of a turn that never ended is kept: the resume window
#: of its checkpoint (``turn_checkpoint.RESUME_MAX_AGE``).
OPEN_TURN_TTL_SECONDS = 24 * 3600


def open_turn_key(conversation_id: str) -> str:
    """Redis key of the record a web turn keeps until it ends."""
    return f"chat:turn:open:{conversation_id}"


@dataclass(frozen=True)
class InterruptedTurn:
    """A turn that took the conversation's lock and never reached its end."""

    #: Its lock token: the turn its frames named (``public_turn_id``).
    token: str
    started_at: datetime | None
    #: When its producer closed it with the interrupted terminal; ``None`` when
    #: its process stopped without a word and its lock lapsed.
    interrupted_at: datetime | None


def _decoded(value: object) -> str | None:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value if isinstance(value, str) else None


def _open_turn_record(raw: object) -> dict | None:
    """The stored record, or ``None`` for anything that is not one."""
    text = _decoded(raw)
    if not text:
        return None
    try:
        record = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("token"), str):
        return None
    return record


def _epoch_moment(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


async def _record_open_turn(r: Any, conversation_id: str, token: str) -> None:
    try:
        await r.set(
            open_turn_key(conversation_id),
            json.dumps({"token": token, "started_at": time.time()}),
            ex=OPEN_TURN_TTL_SECONDS,
        )
    except Exception:
        logger.debug(
            "Could not record the turn of conversation %s", conversation_id, exc_info=True
        )


async def _settle_open_turn(r: Any, conversation_id: str, token: str) -> None:
    """Forget the record of *token*'s turn unless it was marked interrupted."""
    try:
        key = open_turn_key(conversation_id)
        record = _open_turn_record(await r.get(key))
        if record is not None and record["token"] == token and not record.get("interrupted_at"):
            await r.delete(key)
    except Exception:
        logger.debug(
            "Could not settle the turn record of conversation %s",
            conversation_id,
            exc_info=True,
        )


async def mark_turn_interrupted(conversation_id: str, token: str) -> None:
    """Keep the record of *token*'s turn: it ends without its own end.

    Called by ``TurnPublisher.aclose`` when it closes a turn with the
    interrupted terminal, before the turn's release, which then leaves the
    record in place. Shielded, never raises.
    """

    async def _inner() -> None:
        r = await _redis()
        key = open_turn_key(conversation_id)
        record = _open_turn_record(await r.get(key))
        if record is None or record["token"] != token or record.get("interrupted_at"):
            return
        record["interrupted_at"] = time.time()
        await r.set(key, json.dumps(record), ex=OPEN_TURN_TTL_SECONDS)

    try:
        await _run_shielded(_inner())
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug(
            "Could not mark the turn of conversation %s interrupted",
            conversation_id,
            exc_info=True,
        )


async def interrupted_turn(conversation_id: str) -> InterruptedTurn | None:
    """The conversation's turn that ended without its answer, or ``None``.

    A record whose token no longer holds the lock: released after an
    interrupted close, lapsed with a process that died, or taken over.
    """
    r = await _redis()
    record = _open_turn_record(await r.get(open_turn_key(conversation_id)))
    if record is None:
        return None
    if _decoded(await r.get(processing_key(conversation_id))) == record["token"]:
        return None
    return InterruptedTurn(
        token=record["token"],
        started_at=_epoch_moment(record.get("started_at")),
        interrupted_at=_epoch_moment(record.get("interrupted_at")),
    )


async def dismiss_interrupted_turn(conversation_id: str) -> bool:
    """Forget the conversation's interrupted turn; ``False`` when there is none."""
    r = await _redis()
    key = open_turn_key(conversation_id)
    record = _open_turn_record(await r.get(key))
    if record is None:
        return False
    if _decoded(await r.get(processing_key(conversation_id))) == record["token"]:
        return False
    await r.delete(key)
    return True


# Atomic compare-and-refresh: extend the TTL while we still own the
# marker, re-create it when it vanished (Redis blip / eviction), and do
# NOTHING when another turn legitimately took over after a TTL lapse — a
# non-atomic GET-then-write would race the takeover and clobber the new
# holder's token, silently transferring ownership back to a dying turn.
_REFRESH_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
if current == false then
    redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
    return 1
end
return 0
"""

# Atomic compare-and-delete: release only while the stored token is
# still ours (a GET-then-DEL would race a TTL lapse + takeover and
# delete the NEW holder's lock, reopening the concurrent-loop bug).
_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


async def refresh_turn(conversation_id: str, token: str) -> None:
    """Keep the turn marker alive while the turn runs.

    Atomic (single-key Lua): extends the TTL while *token* still owns
    the marker, re-creates a vanished marker, and never clobbers a
    DIFFERENT holder — if another turn acquired the key after our TTL
    lapsed, ownership has legitimately moved on.
    """
    r = await _redis()
    await r.eval(
        _REFRESH_SCRIPT,
        1,
        processing_key(conversation_id),
        token,
        TURN_MARKER_TTL_SECONDS,
    )
    # A pause can outlast several marker TTLs — the reviewer is a person. Carry
    # the pending proposals on the same heartbeat so they never expire out from
    # under a turn that is still waiting for them. No-op when nothing is paused.
    await r.expire(pending_approvals_key(conversation_id), TURN_MARKER_TTL_SECONDS)


async def _release_turn_inner(conversation_id: str, token: str, *, hand_over: bool = True) -> bool:
    worker_shutdown.turn_ended(token)
    r = await _redis()
    # Before the lock goes, while no other turn can write the record.
    await _settle_open_turn(r, conversation_id, token)
    removed = await r.eval(
        _RELEASE_SCRIPT,
        1,
        processing_key(conversation_id),
        token,
    )
    if removed and hand_over:
        await _hand_over_to_queued_channel_messages(r, conversation_id)
    return bool(removed)


async def _hand_over_to_queued_channel_messages(r: Any, conversation_id: str) -> None:
    """Start the next queued Slack / Teams message once a turn released the lock.

    Every turn of every surface ends here, whichever way it ended, so this is
    the one place a queued channel message is handed the conversation. Never
    raises: a release must not fail because of the queue.
    """
    try:
        waiting = await r.llen(channel_queue_key(conversation_id))
        if not isinstance(waiting, int) or waiting <= 0:
            # A claim left by a holder that died: the next drain puts it back.
            waiting = await r.llen(channel_queue_claim_key(conversation_id))
        if not isinstance(waiting, int) or waiting <= 0:
            return
        from app.services.channel_continuation import schedule_queue_drain

        schedule_queue_drain(conversation_id)
    except Exception:
        logger.debug(
            "Could not hand conversation %s to its queued channel messages",
            conversation_id,
            exc_info=True,
        )


async def release_turn(conversation_id: str, token: str, *, hand_over: bool = True) -> bool:
    """Release the turn marker if (and only if) we still own it.

    Atomic (single-key Lua compare-and-delete).  Returns ``True`` when
    the marker was ours and has been deleted, ``False`` when ownership
    had already moved on (TTL lapse followed by another turn's acquire)
    or the marker was already gone.

    Shielded from caller cancellation: release runs from teardown paths
    that may themselves be cancelled (client disconnect) — an
    interrupted release would strand the lock for a full TTL (see
    :func:`_run_shielded`).

    Without *hand_over*, a queued channel message waits for the scheduler's
    sweep instead of starting now: a queue drain that failed does not retry
    in a loop.
    """
    return await _run_shielded(_release_turn_inner(conversation_id, token, hand_over=hand_over))


async def is_turn_active(conversation_id: str) -> bool:
    """True when a turn currently holds the conversation's lock."""
    r = await _redis()
    return bool(await r.exists(processing_key(conversation_id)))


async def channel_turn_holder(conversation_id: str) -> str | None:
    """The channel whose message the conversation's in-flight turn answers.

    ``None`` when no turn runs or the one running belongs to the web. A web
    send checks it before storing anything: a channel turn has no
    steering bridge, so a message folded into it would only be answered by the
    next turn, and its reply goes to the channel, not to the web.
    """
    r = await _redis()
    return channel_turn_token_channel(await r.get(processing_key(conversation_id)))


async def any_turn_active(conversation_ids: Iterable[str]) -> bool:
    """True when at least one of the conversations has a turn in flight.

    One ``EXISTS`` over every marker (Redis returns the count of keys that
    exist), so a caller holding a handful of conversations - the studio
    presence heartbeat reading a user's chats bound to one deck -
    pays a single round-trip. Empty input is ``False`` without a call.
    """
    keys = [processing_key(cid) for cid in conversation_ids]
    if not keys:
        return False
    r = await _redis()
    return bool(await r.exists(*keys))


# ── Cross-process turn cancel ────────────────────────────────
# The cancel route reaches a turn running on its own process directly,
# but the turn may run on another replica, and REST / parent-wake turns
# have no connection to inject into.  The Stop button must
# reach the conversation's in-flight turn WHEREVER it runs, so the
# request travels through Redis and is consumed by the turn's steering
# bridge on its next poll tick (``BRIDGE_POLL_SECONDS``).
#
# The request is BOUND to the current marker token: the consumer only
# honours a request naming its own token, so a stale request (set just
# as the turn finished, or left behind by a crash) can never kill an
# unrelated future turn on the same conversation.  Consumption is a
# GETDEL — single consumer, no second bridge can act on it twice.

# Short-lived: the in-flight turn's bridge polls every second, so an
# unconsumed request means the target turn is already gone.
CANCEL_REQUEST_TTL_SECONDS = 60


def cancel_request_key(conversation_id: str) -> str:
    """Redis key of the per-conversation cross-process cancel request."""
    return f"chat:cancel:{conversation_id}"


async def _address_inflight_turn(key: str, conversation_id: str, ttl: int) -> bool:
    """Store a request at *key* naming the conversation's in-flight turn (``False``: none)."""
    r = await _redis()
    holder = await r.get(processing_key(conversation_id))
    if not holder:
        return False
    if isinstance(holder, bytes):
        holder = holder.decode()
    await r.set(key, holder, ex=ttl)
    return True


async def _consume_turn_request(key: str, token: str) -> bool:
    """Consume the request at *key*: ``True`` when it named the turn holding *token*."""
    r = await _redis()
    value = await r.getdel(key)
    if value is None:
        return False
    if isinstance(value, bytes):
        value = value.decode()
    return value == token


async def request_turn_cancel(
    conversation_id: str, *, client_id: str | None = None, keep_for_send: bool = False
) -> bool:
    """Ask the conversation's in-flight turn to cancel, wherever it runs.

    Reads the current marker token and stores it as the cancel request's
    value, addressing the request to that specific turn.  Returns
    ``True`` when a turn was in flight and the request was stored,
    ``False`` when the conversation is idle (nothing to cancel).

    The read + set pair is deliberately not atomic: if the turn releases
    in between, the request names a token nobody holds any more and is
    discarded by :func:`consume_turn_cancel` (or expires) — the benign
    outcome.

    With *client_id* (the web chat's Stop), a turn found drops the Stop kept
    for that client: the Stop reached a turn. With no turn, the Stop is kept
    for that client's send still on its way to the lock
    (:func:`consume_pending_cancel`) only when *keep_for_send* says the client
    has one; otherwise any kept one goes too, so a Stop that came as a turn
    ended never ends the client's next send. One script, so a send taking the
    lock meanwhile finds either the kept Stop or a request naming its own token.
    """
    if client_id is None:
        return await _address_inflight_turn(
            cancel_request_key(conversation_id), conversation_id, CANCEL_REQUEST_TTL_SECONDS
        )
    r = await _redis()
    return bool(
        await r.eval(
            _REQUEST_CANCEL_LUA,
            3,
            processing_key(conversation_id),
            cancel_request_key(conversation_id),
            pending_cancel_key(conversation_id, client_id),
            str(CANCEL_REQUEST_TTL_SECONDS),
            str(PENDING_CANCEL_TTL_SECONDS),
            "1" if keep_for_send else "0",
        )
    )


async def consume_turn_cancel(conversation_id: str, token: str) -> bool:
    """Consume a pending cancel request addressed to *this* turn.

    Returns ``True`` when a request naming *token* existed (it is
    removed either way — a request naming a different token belongs to a
    turn that no longer holds the lock, so dropping it is correct and
    protects the CURRENT turn from a stale kill).
    """
    return await _consume_turn_request(cancel_request_key(conversation_id), token)


# A web Stop that found no turn while the client's own send is still on its
# way to the lock. Short, so a Stop whose send never comes cannot end a later one.
PENDING_CANCEL_TTL_SECONDS = 30


def pending_cancel_key(conversation_id: str, client_id: str) -> str:
    """Redis key of a Stop kept for *client_id*'s send that has not taken the lock yet."""
    return f"chat:cancel:pending:{conversation_id}:{client_id}"


# KEYS: processing marker, cancel request, kept Stop; ARGV: their TTLs, then
# "1" to keep the Stop when no turn holds the lock. 1 when a turn holds the
# lock (the request names it), 0 when none does.
_REQUEST_CANCEL_LUA = """
local holder = redis.call('GET', KEYS[1])
if holder then
    redis.call('SET', KEYS[2], holder, 'EX', tonumber(ARGV[1]))
    redis.call('DEL', KEYS[3])
    return 1
end
if ARGV[3] == '1' then
    redis.call('SET', KEYS[3], '1', 'EX', tonumber(ARGV[2]))
else
    redis.call('DEL', KEYS[3])
end
return 0
"""


async def consume_pending_cancel(conversation_id: str, client_id: str) -> bool:
    """Take the Stop kept for *client_id* (:func:`request_turn_cancel`): ``True`` when there was one.

    Only a send that just took the lock calls it. A send that starts no turn
    of its own (refused, or folded into another's) and a Stop that reaches a
    turn drop it (:func:`drop_pending_cancel`).
    """
    r = await _redis()
    return await r.getdel(pending_cancel_key(conversation_id, client_id)) is not None


async def drop_pending_cancel(conversation_id: str, client_id: str) -> None:
    """Forget the Stop kept for *client_id*: it must not end the client's next send."""
    r = await _redis()
    await r.delete(pending_cancel_key(conversation_id, client_id))


# ── Cross-process wait skip ──────────────────────────────────
# "Continue now" ends the CURRENT pause of the conversation's turn. Like a
# Stop, it arrives on one request (the web stream's skip-wait route), while the
# turn may run elsewhere: a turn resumed after its worker stopped, a parent
# wake, a REST turn, or a web turn on another process. The request travels the
# same way as the cancel request: bound to the current marker token, consumed
# by the turn's steering bridge (GETDEL) on its next poll tick, which sets the
# loop's ``wait_skip_event``. One skip ends one wait: a request consumed while
# no wait runs is cleared by the next ``execute_wait`` like a same-socket skip.

WAIT_SKIP_REQUEST_TTL_SECONDS = CANCEL_REQUEST_TTL_SECONDS


def wait_skip_request_key(conversation_id: str) -> str:
    """Redis key of the per-conversation cross-process wait-skip request."""
    return f"chat:skipwait:{conversation_id}"


async def request_wait_skip(conversation_id: str) -> bool:
    """Ask the conversation's in-flight turn to end its current pause.

    Returns ``True`` when a turn was in flight and the request was stored,
    ``False`` when the conversation is idle (no pause to end). The same
    benign race as :func:`request_turn_cancel`: a request naming a token
    nobody holds any more is discarded by :func:`consume_wait_skip`.
    """
    return await _address_inflight_turn(
        wait_skip_request_key(conversation_id), conversation_id, WAIT_SKIP_REQUEST_TTL_SECONDS
    )


async def consume_wait_skip(conversation_id: str, token: str) -> bool:
    """Consume a pending wait-skip request addressed to *this* turn.

    ``True`` when a request naming *token* existed; a request naming
    another token belongs to a turn that is gone and is dropped.
    """
    return await _consume_turn_request(wait_skip_request_key(conversation_id), token)


# How long the Stop's moment stays in Redis. It only has to outlive the gap
# until the cancel cascade commits ``Conversation.stopped_at``, which is the
# durable record; the inbox TTL is ample.
STOP_RECORD_TTL_SECONDS = STEERING_INBOX_TTL_SECONDS


def stop_record_key(conversation_id: str) -> str:
    """Redis key of the moment the user last stopped this conversation."""
    return f"chat:stopped:{conversation_id}"


def _epoch_micros(moment: datetime) -> int:
    """*moment* as microseconds since the epoch, the Stop record's unit.

    One conversion for the record and for what is compared with it, so a Stop
    and a submission at the same instant compare equal in Lua.
    """
    return int(moment.timestamp() * 1_000_000)


def _from_epoch_micros(value: object) -> datetime | None:
    """The moment :func:`_epoch_micros` wrote, or ``None`` for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, str, bytes)):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1_000_000, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# Only ever moves forward, like the database's GREATEST: two Stops whose
# writes land out of order keep the later moment. Microseconds since the epoch.
_RECORD_STOP_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if tonumber(ARGV[1]) > current then
    redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
end
return 1
"""

# A partner's answer is pushed only while no Stop at or after its work's
# submission is recorded: read and push in one script, so a Stop
# recorded concurrently lands wholly before the push, which it refuses, or
# after it, where the drains re-check the entry. Returns 1 when pushed.
_FOLD_UNLESS_STOPPED_LUA = """
local stopped = tonumber(redis.call('GET', KEYS[2]) or '0')
if stopped >= tonumber(ARGV[2]) then
    return 0
end
redis.call('RPUSH', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return 1
"""


class FoldRefusedError(Exception):
    """A partner's answer the user stopped before it reached the running turn.

    Raised by :func:`fold_into_inflight_turn`, and out of
    :func:`acquire_or_fold`, when a Stop at or after the work's submission is
    recorded. Nothing was pushed: the answer was persisted before the
    hand-off, so the next turn the user starts reads it in its history.
    """


async def record_stop(conversation_id: str, stopped_at: datetime) -> None:
    """Record the Stop's moment on the acknowledgement path.

    Written before the cross-process cancel request, so a partner wake that
    takes the turn after that request found no marker still reads the Stop
    when it re-checks, even though the cascade has not committed
    ``Conversation.stopped_at`` yet. Never moves back: an earlier Stop landing
    late leaves the later one in place.
    """
    r = await _redis()
    await r.eval(
        _RECORD_STOP_LUA,
        1,
        stop_record_key(conversation_id),
        str(_epoch_micros(stopped_at)),
        str(STOP_RECORD_TTL_SECONDS),
    )


# Deletes the record only while it still holds this Stop's moment: a later
# Stop recorded meanwhile stays.
_FORGET_STOP_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('DEL', KEYS[1])
end
return 1
"""


async def forget_stop(conversation_id: str, stopped_at: datetime) -> None:
    """Withdraw the record :func:`record_stop` wrote for a Stop that reached nothing.

    An earlier Stop it superseded goes with it; that one's cascade commits
    ``Conversation.stopped_at``, which the drains read as well.
    """
    r = await _redis()
    await r.eval(_FORGET_STOP_LUA, 1, stop_record_key(conversation_id), str(_epoch_micros(stopped_at)))


async def recorded_stop(conversation_id: str) -> datetime | None:
    """The Stop's moment recorded by :func:`record_stop`, or ``None``."""
    r = await _redis()
    value = await r.get(stop_record_key(conversation_id))
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode()
    return _from_epoch_micros(value)


def _encode_entry(
    text: str,
    file_ids: list[str] | None = None,
    context_file_ids: list[str] | None = None,
    partner_wake: bool = False,
    delegated_at: datetime | None = None,
    *,
    mentioned_agent_ids: list[str] | None = None,
    mentions_only: bool = False,
    platform_text: bool = False,
    message_id: str | None = None,
) -> str:
    entry: dict = {
        "id": uuid.uuid4().hex,
        "text": text,
        "file_ids": list(file_ids or []),
    }
    # Written only when set, so an entry an older replica reads mid-deploy
    # is the shape it already knows.
    if context_file_ids:
        entry["context_file_ids"] = list(context_file_ids)
    if partner_wake:
        entry["partner_wake"] = True
    if platform_text:
        entry["platform_text"] = True
    if message_id:
        entry["message_id"] = message_id
    if delegated_at is not None:
        entry["delegated_at"] = _epoch_micros(delegated_at)
    # Same for an older replica: it injects the text as it always did.
    if mentioned_agent_ids:
        entry["mentioned_agent_ids"] = list(mentioned_agent_ids)
        if mentions_only:
            entry["mentions_only"] = True
            # A hand-back's text is in the thread already. Kept under a key an
            # older replica does not read, so one draining it mid-deploy
            # skips it as malformed instead of injecting or following up the
            # message again.
            entry["owed_text"] = entry.pop("text")
    return json.dumps(entry)


def _str_list(raw: object) -> list[str]:
    """The string entries of *raw* when it is a list; nothing otherwise.

    Guards the container shape too: a non-list ``file_ids`` would raise in
    the decoder and poison the whole drain, which is exactly what it
    promises not to do.
    """
    if not isinstance(raw, list):
        return []
    return [f for f in raw if isinstance(f, str)]


def _decode_entries(raw_items: list[str]) -> list[SteeringPayload]:
    """Parse raw inbox payloads back into steering payloads.

    Malformed entries (should not happen — we are the only writer) are
    skipped with a warning rather than poisoning the drain.
    """
    payloads: list[SteeringPayload] = []
    for raw in raw_items:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict):
            text = data.get("text")
            file_ids = data.get("file_ids")
            context_file_ids = data.get("context_file_ids")
            partner_wake = data.get("partner_wake") is True
            delegated_at = _from_epoch_micros(data.get("delegated_at"))
            mentioned_agent_ids = data.get("mentioned_agent_ids")
            mentions_only = data.get("mentions_only") is True
            platform_text = data.get("platform_text") is True
            message_id = data.get("message_id")
            if mentions_only and text is None:
                text = data.get("owed_text")
        else:
            # A non-dict JSON value (array / string / number) would raise
            # ``AttributeError`` on ``.get`` — not covered by the except
            # above — and poison the drain.
            text = None
            file_ids = None
            context_file_ids = None
            partner_wake = False
            delegated_at = None
            mentioned_agent_ids = None
            mentions_only = False
            platform_text = False
            message_id = None
        if isinstance(text, str) and text.strip():
            mentioned = _str_list(mentioned_agent_ids)
            payloads.append(
                SteeringPayload(
                    text=text,
                    file_ids=_str_list(file_ids),
                    context_file_ids=_str_list(context_file_ids),
                    partner_wake=partner_wake,
                    delegated_at=delegated_at,
                    mentioned_agent_ids=mentioned,
                    mentions_only=mentions_only and bool(mentioned),
                    platform_text=platform_text,
                    message_id=message_id if isinstance(message_id, str) else None,
                )
            )
        else:
            logger.warning(
                "Skipping malformed steering inbox entry (%d chars)",
                len(raw or ""),
            )
    return payloads


async def fold_into_inflight_turn(
    conversation_id: str,
    text: str,
    file_ids: list[str] | None = None,
    context_file_ids: list[str] | None = None,
    partner_wake: bool = False,
    delegated_at: datetime | None = None,
    *,
    mentioned_agent_ids: list[str] | None = None,
    accepted_by: list[str] | None = None,
    platform_text: bool = False,
    message_id: str | None = None,
) -> bool:
    """Hand *text* to the conversation's in-flight turn instead of
    starting a second agent loop.

    The caller MUST have persisted *text* as a regular user message
    first — the inbox is a delivery channel, not a store of record.

    Returns ``True`` when the running turn is guaranteed to consume the
    text (mid-run injection or its exit-drain follow-up pass), and
    ``False`` when the in-flight turn finished before the hand-off could
    complete — the caller must then run its own turn (the push has been
    recovered, nothing is left behind).

    A partner's answer carrying *delegated_at* is pushed in one Lua call
    with the Stop check, and raises :class:`FoldRefusedError` when a Stop at
    or after *delegated_at* is recorded.

    *accepted_by*, when given, receives the token of the turn whose marker
    the check saw - the turn that consumes the text - so the caller can
    name that turn without reading the lock again: by then it may
    be released, or held by the next turn. Nothing is appended when the
    exiting turn's release-drain took the entry: that turn is gone.
    """
    r = await _redis()
    inbox = steering_inbox_key(conversation_id)
    payload = _encode_entry(
        text,
        file_ids,
        context_file_ids,
        partner_wake,
        delegated_at,
        mentioned_agent_ids=mentioned_agent_ids,
        platform_text=platform_text,
        message_id=message_id,
    )
    if partner_wake and delegated_at is not None:
        pushed = await r.eval(
            _FOLD_UNLESS_STOPPED_LUA,
            2,
            inbox,
            stop_record_key(conversation_id),
            payload,
            str(_epoch_micros(delegated_at)),
            str(STEERING_INBOX_TTL_SECONDS),
        )
        if not pushed:
            raise FoldRefusedError(
                f"conversation {conversation_id} was stopped at or after the "
                "moment the work this answer delivers was sent"
            )
    else:
        await r.rpush(inbox, payload)
        await r.expire(inbox, STEERING_INBOX_TTL_SECONDS)
    holder = await r.get(processing_key(conversation_id))
    if holder:
        # The running turn's exit path drains the inbox AGAIN after
        # releasing the marker, so an entry pushed while the marker is
        # visible is always consumed.
        if accepted_by is not None:
            accepted_by.append(holder.decode() if isinstance(holder, bytes) else str(holder))
        return True
    # The turn released between our failed acquire and the push.  Try to
    # take the entry back: LREM from the tail (ours is the most recent
    # occurrence of this exact payload).  When the LREM finds nothing,
    # someone consumed the entry first (the exiting turn's release-drain)
    # and it will be answered in that turn's follow-up pass.
    removed = await r.lrem(inbox, -1, payload)
    return not removed


async def _discard_stale_inbox(
    conversation_id: str, purged_mentions: list[SteeringPayload] | None = None
) -> list[str]:
    """Purge inbox entries left behind by a previous turn.

    Runs exactly once per turn, at lock acquisition — which precedes the
    turn's history snapshot load AND its bridge task in every entry
    point (the web chat's send, REST ``send_message``, parent wake).  Every fold
    persists its text as a regular user message BEFORE pushing it, so
    anything still in the inbox when a NEW turn acquires the lock is
    already part of the history this turn is about to load.  Its only
    consumer died with the previous turn (crashed marker) or never
    existed (:func:`requeue_stranded`); left in place, the new turn's
    bridge would drain it on the first poll tick and inject it AGAIN as
    a steering user message at iteration 1 — ordered after the new user
    message, so the model reads a stale instruction as the most recent
    one.

    Returns the ``context_file_ids`` the purged entries carried:
    the TEXT of a purged wake reaches the new turn through history, but
    the files it announces exist only on the inbox entry - the hidden
    wake message deliberately carries none, since a user message's
    files are inlined by every provider. The acquiring turn registers
    them in its FileContext (:func:`acquire_or_fold`'s
    ``purged_context_file_ids``), so a wake that lost the hand-off race
    still delivers its files to the turn that reads it. Empty on a
    failed purge. The purged entries that name agents are appended to
    *purged_mentions* for the same reason: the message is in
    history, the agents it mentions are owed an answer nobody else will
    give.

    No legitimately-new fold can be discarded here: a fold is only
    pushed after a FAILED acquire, and a fold that races this turn's
    successful acquire pushed an entry whose text was persisted before
    the push — hence before this turn's history load — so this turn
    still answers it exactly once, as part of history.  (A racing fold
    that pushes AFTER this purge is bridged as steering like any mid-run
    fold; whether its persisted copy also lands in this turn's history
    snapshot is the pre-existing acquisition-boundary race, which
    this purge narrows but does not claim to close.)

    Best-effort by contract: the caller has already acquired the lock,
    and raising here would strand it (the token never reaches the
    caller), which is strictly worse than the rare double delivery this
    purge prevents.  Cancellation is deliberately NOT swallowed here —
    it must propagate — so :func:`acquire_or_fold` releases the
    just-acquired lock before letting it escape.
    """
    try:
        purged = await drain_steering_inbox(conversation_id)
    except Exception:
        logger.warning(
            "Failed to purge the steering inbox at turn acquisition for "
            "conversation %s — a stale entry may be re-injected",
            conversation_id,
            exc_info=True,
        )
        return []
    if purged:
        logger.info(
            "Discarded %d stale steering payload(s) (%d chars) for "
            "conversation %s already persisted in history",
            len(purged),
            sum(len(p.text) for p in purged),
            conversation_id,
        )
    if purged_mentions is not None:
        purged_mentions.extend(p for p in purged if p.mentioned_agent_ids)
    seen: set[str] = set()
    carried: list[str] = []
    for payload in purged:
        for file_id in payload.context_file_ids:
            if file_id not in seen:
                seen.add(file_id)
                carried.append(file_id)
    return carried


async def acquire_or_fold(
    conversation_id: str,
    text: str,
    file_ids: list[str] | None = None,
    context_file_ids: list[str] | None = None,
    *,
    purged_context_file_ids: list[str] | None = None,
    partner_wake: bool = False,
    delegated_at: datetime | None = None,
    mentioned_agent_ids: list[str] | None = None,
    purged_mentions: list[SteeringPayload] | None = None,
    accepted_by: list[str] | None = None,
    platform_text: bool = False,
) -> tuple[str | None, bool]:
    """Become the conversation's turn, or fold *text* into the one running.

    The full entry-point handshake, shared by the web chat's send, REST
    ``send_message`` and the parent wake.  Each round first tries
    :func:`acquire_turn`; on failure it tries :func:`fold_into_inflight_turn`
    (carrying *file_ids*, *context_file_ids*, *partner_wake*, *delegated_at*
    and *mentioned_agent_ids* - see :class:`SteeringPayload` for their
    meanings).  A failed fold means the in-flight turn finished during the
    hand-off — the next round retries the acquire (the lock is likely free
    now).  A refused one - a partner's answer the user stopped -
    raises :class:`FoldRefusedError` out of this call, with no lock held.
    The caller
    MUST have persisted *text* as a regular user message first.

    A successful acquire purges the steering inbox before returning:
    entries a previous turn never consumed are already
    persisted user messages, so they reach this turn through the history
    it loads next — re-injecting them as steering would deliver them
    twice (see :func:`_discard_stale_inbox`).  The ``context_file_ids``
    those purged entries carried are appended to *purged_context_file_ids*
    when the caller passes a list: the files a purged wake
    announced live nowhere else, and the turn that acquires must register
    them in its FileContext once it has one (``register_context_files``)
    or it reads a text saying they are attached while holding none.  The
    purged entries that name agents are appended to *purged_mentions*:
    the acquiring turn answers them with those agents.
    *accepted_by* is handed to :func:`fold_into_inflight_turn`: on a fold,
    it receives the token of the turn that took the message.

    Returns ``(token, folded)``:

    * ``(token, False)`` — we own the lock; run the turn and release it.
    * ``(None, True)`` — folded; the running turn answers the message.
    * ``(None, False)`` — pathological hand-off alternation
      (``ACQUIRE_FOLD_ATTEMPTS`` turns started AND finished inside our
      hand-off windows).  The already-persisted message was requeued via
      :func:`requeue_stranded`: a turn already running consumes it live
      via its bridge, otherwise it shapes the next turn through history
      (whose acquisition discards the inbox copy).
    """
    for _ in range(ACQUIRE_FOLD_ATTEMPTS):
        token = await acquire_turn(conversation_id)
        if token is not None:
            try:
                purged_ids = await _discard_stale_inbox(
                    conversation_id, purged_mentions
                )
                if purged_context_file_ids is not None:
                    purged_context_file_ids.extend(purged_ids)
            except BaseException:
                # ``_discard_stale_inbox`` swallows every ``Exception``,
                # so only a ``CancelledError`` (client disconnect, worker
                # shutdown) — or another BaseException — can land here.
                # The caller never receives the token on this path, so
                # nothing downstream would release the lock: release it
                # ourselves (cancellation-shielded) before propagating,
                # or it strands for a full TTL and every send in that
                # window folds into a turn that no longer exists.
                await release_turn(conversation_id, token)
                raise
            return token, False
        if await fold_into_inflight_turn(
            conversation_id,
            text,
            file_ids=file_ids,
            context_file_ids=context_file_ids,
            partner_wake=partner_wake,
            delegated_at=delegated_at,
            mentioned_agent_ids=mentioned_agent_ids,
            accepted_by=accepted_by,
            platform_text=platform_text,
        ):
            return None, True
    logger.warning(
        "Turn hand-off for conversation %s kept racing after %d attempts — "
        "requeueing the (already-persisted) message for the next turn",
        conversation_id,
        ACQUIRE_FOLD_ATTEMPTS,
    )
    await requeue_stranded(
        conversation_id,
        [
            SteeringPayload(
                text=text,
                file_ids=list(file_ids or []),
                context_file_ids=list(context_file_ids or []),
                partner_wake=partner_wake,
                delegated_at=delegated_at,
                mentioned_agent_ids=list(mentioned_agent_ids or []),
                platform_text=platform_text,
            )
        ],
    )
    return None, False


async def drain_steering_inbox(conversation_id: str) -> list[SteeringPayload]:
    """Non-blockingly drain every pending steering payload from the inbox."""
    r = await _redis()
    inbox = steering_inbox_key(conversation_id)
    raw_items: list[str] = []
    while True:
        item = await r.lpop(inbox)
        if item is None:
            break
        raw_items.append(item)
    return _decode_entries(raw_items)


async def release_turn_and_drain(
    conversation_id: str, token: str, *, hand_over: bool = True
) -> list[SteeringPayload]:
    """Turn-exit handshake: release the lock, then catch racing folds.

    Any text returned here was folded at the exact moment the turn was
    finishing (after its last regular inbox drain), and the fold told its
    sender the turn took it.  The caller answers it in a follow-up turn
    once the lock is released (``parent_wake.schedule_fold_follow_up``,
    which takes the conversation or folds the text into the turn that took
    it over), or hands it on with :func:`requeue_stranded`.

    The drain is gated on lock ownership: when *token* no longer owns
    the marker (our TTL lapsed and another turn legitimately acquired
    it), the pending inbox entries belong to the NEW holder's running
    loop — consuming them here would break the exactly-once fold
    guarantee, so an empty list is returned and the entries are left
    for the new holder's bridge.

    The release + drain pair is shielded as ONE unit from caller
    cancellation (see :func:`_run_shielded`): it runs from teardown
    paths that may themselves be cancelled, and an interruption between
    the release and the drain would strand a racing fold in the inbox
    with no holder to consume it.

    Without *hand_over*, a queued channel message waits for the scheduler's
    sweep (:func:`release_turn`): a turn cut by a shutdown leaves its resume to
    the next process rather than to the one stopping.
    """

    async def _inner() -> list[SteeringPayload]:
        owned = await _release_turn_inner(conversation_id, token, hand_over=hand_over)
        if not owned:
            return []
        return await drain_steering_inbox(conversation_id)

    return await _run_shielded(_inner())


async def requeue_stranded(
    conversation_id: str, payloads: list[SteeringPayload]
) -> None:
    """Push stranded steering payloads back for the new lock holder.

    Only a turn ALREADY holding the lock consumes these live (its bridge
    drains the inbox while it runs).  If no turn is running, the next
    acquisition discards them — they are persisted user
    messages, so they shape that turn through the history it loads
    instead of being re-injected as steering — and hands the ones that
    name agents to that turn (``purged_mentions``).
    """
    if not payloads:
        return
    r = await _redis()
    inbox = steering_inbox_key(conversation_id)
    for payload in payloads:
        await r.rpush(
            inbox,
            _encode_entry(
                payload.text,
                payload.file_ids,
                payload.context_file_ids,
                payload.partner_wake,
                payload.delegated_at,
                mentioned_agent_ids=payload.mentioned_agent_ids,
                mentions_only=payload.mentions_only,
                platform_text=payload.platform_text,
            ),
        )
    await r.expire(inbox, STEERING_INBOX_TTL_SECONDS)


async def requeue_owed_mentions(
    conversation_id: str, payloads: Iterable[SteeringPayload]
) -> int:
    """Hand back the folds whose mentioned agents a turn ended without asking.

    Requeued ``mentions_only``: their text is in the thread already,
    so the turn running by then answers those agents without injecting it
    again, and when none runs the next acquisition hands them to the turn it
    starts. Payloads naming no agent are left out. Returns how many were
    requeued.
    """
    owed = [
        replace(payload, mentions_only=True)
        for payload in payloads
        if payload.mentioned_agent_ids
    ]
    if owed:
        await requeue_stranded(conversation_id, owed)
    return len(owed)


# ── Slack / Teams messages queued behind a running turn ──────
# A turn run for a channel message has no steering bridge and answers on its
# channel, so a Slack or Teams direct message that arrives while a turn holds
# the conversation is not folded into it: it waits in a per-conversation list
# and runs as the next turn. Only the holder of the turn lock takes it
# (``channel_continuation.drain_channel_queue`` takes the lock first), so two
# processes never run the same message, and every release of the lock starts
# the drain (``_hand_over_to_queued_channel_messages``). A message taken moves
# to the conversation's claimed list until its turn answers it: a process that
# dies in between leaves it there, and the next holder puts it back.

#: How many messages may wait for one conversation's turn.
CHANNEL_QUEUE_MAX_MESSAGES = 5

#: How long a queue lives after its last message: one nobody drained by then
#: is dropped rather than answered hours late.
CHANNEL_QUEUE_TTL_SECONDS = 3600

#: How long a platform message id is remembered, so a redelivered event is
#: never queued or answered twice.
CHANNEL_MESSAGE_CLAIM_TTL_SECONDS = 6 * 3600

#: The conversations that have, or recently had, queued messages: read by the
#: scheduler's sweep to drain a queue whose lock holder died without releasing.
CHANNEL_QUEUE_INDEX_KEY = "chat:channel_queue:conversations"


def channel_queue_key(conversation_id: str) -> str:
    """Redis key of the messages waiting for the conversation's turn."""
    return f"chat:channel_queue:{conversation_id}"


def channel_queue_claim_key(conversation_id: str) -> str:
    """Redis key of the queued messages the turn holder took and has not answered yet."""
    return f"chat:channel_queue:{conversation_id}:claimed"


def channel_message_claim_key(channel: str, thread_id: str, message_id: str) -> str:
    """Redis key marking a platform message as received."""
    return f"chat:channel_message:{channel}:{thread_id}:{message_id}"


# Append unless the queue is full, in one script: two messages arriving at
# once cannot both take the last place.
_ENQUEUE_CHANNEL_MESSAGE_LUA = """
if redis.call('LLEN', KEYS[1]) >= tonumber(ARGV[2]) then
    return 0
end
local n = redis.call('RPUSH', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return n
"""

# The scripts below act for the turn holder only (KEYS[1] holds ARGV[1]): a
# holder whose lock lapsed never touches what the next one claimed.

# Move the next message to the claimed list in one script: a process that
# dies right after leaves it claimed, never nowhere.
_CLAIM_QUEUED_CHANNEL_MESSAGE_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return false
end
local item = redis.call('LPOP', KEYS[2])
if item then
    redis.call('RPUSH', KEYS[3], item)
    redis.call('EXPIRE', KEYS[3], ARGV[2])
end
return item
"""

# The claimed messages were answered (or refused): forget them.
_ANSWER_CLAIMED_CHANNEL_MESSAGES_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
return redis.call('DEL', KEYS[2])
"""

# Put the claimed messages back at the head of the queue, in their order
# (ARGV[3..]: as rewritten by the caller, the ones it drops left out), and
# forget the claim, in one script.
_REQUEUE_CLAIMED_CHANNEL_MESSAGES_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return -1
end
for i = #ARGV, 3, -1 do
    redis.call('LPUSH', KEYS[2], ARGV[i])
end
if #ARGV > 2 then
    redis.call('EXPIRE', KEYS[2], ARGV[2])
end
redis.call('DEL', KEYS[3])
return #ARGV - 2
"""

# Leave the sweep's index only when nothing waits and nothing is claimed,
# checked with the removal: a message queued meanwhile keeps it.
_FORGET_EMPTY_CHANNEL_QUEUE_LUA = """
if redis.call('LLEN', KEYS[1]) > 0 or redis.call('LLEN', KEYS[2]) > 0 then
    return 0
end
return redis.call('SREM', KEYS[3], ARGV[1])
"""


async def claim_channel_message(channel: str, thread_id: str, message_id: str) -> bool:
    """``True`` the first time a platform message is seen, ``False`` for a redelivery."""
    r = await _redis()
    return bool(
        await r.set(
            channel_message_claim_key(channel, thread_id, message_id),
            "1",
            nx=True,
            ex=CHANNEL_MESSAGE_CLAIM_TTL_SECONDS,
        )
    )


async def enqueue_channel_message(conversation_id: str, item: str) -> int | None:
    """Queue *item* behind the conversation's turn: its place (1-based), ``None`` when full."""
    r = await _redis()
    place = await r.eval(
        _ENQUEUE_CHANNEL_MESSAGE_LUA,
        1,
        channel_queue_key(conversation_id),
        item,
        CHANNEL_QUEUE_MAX_MESSAGES,
        CHANNEL_QUEUE_TTL_SECONDS,
    )
    if not place:
        return None
    try:
        await r.sadd(CHANNEL_QUEUE_INDEX_KEY, conversation_id)
    except Exception:
        logger.debug("Could not index the channel queue of %s", conversation_id, exc_info=True)
    return int(place)


def channel_resume_claim_key(channel: str, thread_id: str, message_id: str) -> str:
    """Redis key counting the automatic resumes of a platform message whose turn a shutdown cut."""
    return f"chat:channel_resume:{channel}:{thread_id}:{message_id}"


async def claim_channel_resume(channel: str, thread_id: str, message_id: str) -> bool:
    """``True`` while a cut turn may hand this platform message on again, ``False`` after.

    The same cap as a web turn's (``cut_turn_resume.MAX_AUTO_RESUMES``): a
    message is put back at most that many times; the next cut tells the
    sender to send it again (a cut the turn itself causes would loop).
    """
    from app.services.cut_turn_resume import MAX_AUTO_RESUMES

    r = await _redis()
    key = channel_resume_claim_key(channel, thread_id, message_id)
    made = int(await r.incr(key))
    if made == 1:
        await r.expire(key, CHANNEL_QUEUE_TTL_SECONDS)
    return made <= MAX_AUTO_RESUMES


async def index_channel_queue(conversation_id: str) -> None:
    """Put the conversation in the sweep's index (:data:`CHANNEL_QUEUE_INDEX_KEY`).

    For messages put back at the head of its queue without :func:`enqueue_channel_message`:
    a turn cut by a shutdown hands its message to the next process's sweep.
    """
    r = await _redis()
    await r.sadd(CHANNEL_QUEUE_INDEX_KEY, conversation_id)


async def channel_queue_length(conversation_id: str) -> int:
    """How many messages wait for the conversation's turn."""
    r = await _redis()
    return int(await r.llen(channel_queue_key(conversation_id)) or 0)


async def peek_channel_queue(conversation_id: str) -> str | None:
    """The message next in line, left in place."""
    r = await _redis()
    return await r.lindex(channel_queue_key(conversation_id), 0)


async def claim_queued_channel_message(conversation_id: str, token: str) -> str | None:
    """Take the message next in line onto the claimed list; ``None`` when none, or *token* no longer holds the turn."""
    r = await _redis()
    return await r.eval(
        _CLAIM_QUEUED_CHANNEL_MESSAGE_LUA,
        3,
        processing_key(conversation_id),
        channel_queue_key(conversation_id),
        channel_queue_claim_key(conversation_id),
        token,
        CHANNEL_QUEUE_TTL_SECONDS,
    )


async def claimed_channel_messages(conversation_id: str) -> list[str]:
    """The queued messages taken and not answered yet, in their order."""
    r = await _redis()
    return list(await r.lrange(channel_queue_claim_key(conversation_id), 0, -1) or [])


async def answer_claimed_channel_messages(conversation_id: str, token: str) -> bool:
    """Forget the claimed messages once their turn answered (or refused) them, before it releases the lock."""
    r = await _redis()
    return bool(
        await r.eval(
            _ANSWER_CLAIMED_CHANNEL_MESSAGES_LUA,
            2,
            processing_key(conversation_id),
            channel_queue_claim_key(conversation_id),
            token,
        )
    )


async def requeue_claimed_channel_messages(conversation_id: str, token: str, items: list[str]) -> bool:
    """Replace the claim with *items* at the head of the queue, in their order; ``False`` when *token* no longer holds the turn."""
    r = await _redis()
    moved = await r.eval(
        _REQUEUE_CLAIMED_CHANNEL_MESSAGES_LUA,
        3,
        processing_key(conversation_id),
        channel_queue_key(conversation_id),
        channel_queue_claim_key(conversation_id),
        token,
        CHANNEL_QUEUE_TTL_SECONDS,
        *items,
    )
    return int(moved) >= 0


async def channel_messages_owed(conversation_id: str) -> bool:
    """Whether a queued message waits, or one taken was never answered (its holder died)."""
    r = await _redis()
    return bool(
        await r.llen(channel_queue_key(conversation_id))
        or await r.llen(channel_queue_claim_key(conversation_id))
    )


async def queued_conversations() -> list[str]:
    """The conversations the sweep looks at (:data:`CHANNEL_QUEUE_INDEX_KEY`)."""
    r = await _redis()
    members = await r.smembers(CHANNEL_QUEUE_INDEX_KEY)
    return [m.decode() if isinstance(m, bytes) else str(m) for m in members or ()]


async def forget_queued_conversation(conversation_id: str) -> bool:
    """Drop a conversation from the sweep's index once nothing waits and nothing is claimed."""
    r = await _redis()
    return bool(
        await r.eval(
            _FORGET_EMPTY_CHANNEL_QUEUE_LUA,
            3,
            channel_queue_key(conversation_id),
            channel_queue_claim_key(conversation_id),
            CHANNEL_QUEUE_INDEX_KEY,
            conversation_id,
        )
    )
