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

"""Messages an assignment's own run posted, so it never triggers itself on them.

An agent that answers in Slack or Teams posts through the integration's token (or a
channel bot's), so its message reads to the poller and to the Slack events endpoint
like any other new message in the channel - and a ``new_slack_message`` /
``new_teams_message`` assignment watching that channel would run on its own reply,
again and again. Every tool or adapter that posts a message records the message's
identity here when the post succeeds, together with the assignment whose run posted
it; both read paths skip a recorded message for THAT assignment only, through the
one lookup of each provider.

Everything else triggers exactly as before: a message a person typed, a chat reply, a
channel bot's answer outside an assignment run, and a post another assignment made
(an assignment watching a channel another one writes to keeps running on it). A post
made outside an assignment run is not recorded at all. The same holds for the mail an
assignment's run sends or drafts through a Microsoft mailbox, which the ``new_email``
poller would otherwise read back from Sent Items and Drafts.

Fail-open both ways: a store failure never blocks a post (the record is logged and
dropped) and never drops a message (an unreadable record reads as "not its own post").
"""

from __future__ import annotations

import hashlib
import logging
import time
from contextvars import ContextVar
from datetime import UTC, datetime

from app.core.redis import redis_client
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

AGENT_POST_TTL_SECONDS = 86400 * 7
"""How long a posted message is remembered: the seen-set claim window.

Far past Slack's retries (minutes) and the pollers' 5-minute cadence, and as long as
the window inside which a message can still reach a trigger without being claimed.
"""

_KEY_PREFIX = "agent_post"

_posting_assignment: ContextVar[str | None] = ContextVar("posting_assignment", default=None)


def set_posting_assignment(assignment_id: object | None):
    """Bind the assignment whose run is executing; returns the token to reset."""
    return _posting_assignment.set(str(assignment_id) if assignment_id else None)


def reset_posting_assignment(token) -> None:
    _posting_assignment.reset(token)


def posting_assignment() -> str | None:
    """The assignment whose run is executing in this context, if any."""
    return _posting_assignment.get()


def agent_post_key(provider: str, *parts: str) -> str:
    """The Redis key naming one message an assignment's run posted."""
    return ":".join((_KEY_PREFIX, provider, *parts))


async def record_agent_post(provider: str, *parts: object) -> None:
    """Remember a message the running assignment posted. Never raises."""
    assignment_id = posting_assignment()
    if not assignment_id:
        return
    names = [str(p) for p in parts if p]
    if len(names) != len(parts):
        return
    try:
        redis = await redis_client()
        await redis.set(agent_post_key(provider, *names), assignment_id, ex=AGENT_POST_TTL_SECONDS)
    except Exception as exc:
        logger.warning(
            "Could not record an agent-posted %s message (its assignment may run on it): %s",
            provider,
            format_exception_message(exc),
        )


async def is_agent_post(redis, provider: str, *parts: object, assignment_id: object) -> bool:
    """Whether *assignment_id*'s own run posted a message. ``False`` when it cannot be told."""
    names = [str(p) for p in parts if p]
    if redis is None or not assignment_id or len(names) != len(parts):
        return False
    try:
        poster = await redis.get(agent_post_key(provider, *names))
    except Exception as exc:
        logger.debug("Agent post lookup failed (%s): %s", provider, format_exception_message(exc))
        return False
    if isinstance(poster, bytes):
        poster = poster.decode("utf-8", "replace")
    return poster == str(assignment_id)


# ---------------------------------------------------------------------------
# Slack: a message is its channel id and its ts
# ---------------------------------------------------------------------------


async def record_slack_post(response: dict) -> None:
    """Record the message a successful ``chat.postMessage`` answer names."""
    if isinstance(response, dict) and response.get("ok"):
        await record_agent_post("slack", response.get("channel"), response.get("ts"))


async def record_slack_file_shares(file: dict) -> None:
    """Record the messages a ``files.upload`` posted (one per channel it was shared in)."""
    shares = (file or {}).get("shares") if isinstance(file, dict) else None
    if not isinstance(shares, dict):
        return
    for scope in shares.values():
        if not isinstance(scope, dict):
            continue
        for channel, posts in scope.items():
            for post in posts if isinstance(posts, list) else []:
                if isinstance(post, dict):
                    await record_agent_post("slack", channel, post.get("ts"))


async def is_agent_slack_post(redis, channel: str, ts: str, assignment_id: object) -> bool:
    """The one lookup of the Slack poller and the Slack events endpoint."""
    return await is_agent_post(redis, "slack", channel, ts, assignment_id=assignment_id)


# ---------------------------------------------------------------------------
# Teams: a channel message is its team, its channel and its Graph id
# ---------------------------------------------------------------------------


async def record_teams_channel_post(team_id: str, channel_id: str, response: dict) -> None:
    """Record the channel message a successful Graph ``POST .../messages`` created."""
    if isinstance(response, dict):
        await record_agent_post("teams", team_id, channel_id, response.get("id"))


async def is_agent_teams_post(redis, team_id: str, channel_id: str, message_id: str, assignment_id: object) -> bool:
    """The lookup of the Teams channel poller."""
    return await is_agent_post(redis, "teams", team_id, channel_id, message_id, assignment_id=assignment_id)


# ---------------------------------------------------------------------------
# Microsoft mail: a draft is its Graph id; a send is what it is
# ---------------------------------------------------------------------------
#
# ``sendMail`` answers 202 with no body, and the copy Exchange files in Sent Items
# gets an id of its own, so a send is recorded as its subject and recipients, the
# time it was sent, the integration (mailbox) that sent it and the assignment whose
# run sent it. Nothing is added to the mail itself: a header or an extended property
# would travel to recipients.

MAIL_SEND_MATCH_SECONDS = 600
"""How far a message's ``sentDateTime`` may be from a recorded send and still be it."""


def mail_fingerprint(subject: object, recipients: list[object]) -> str:
    """One mail's subject and To / Cc addresses, as the Sent Items copy repeats them."""
    addresses = sorted({str(a).strip().lower() for a in recipients if str(a or "").strip()})
    normalized = " ".join(str(subject or "").split()).lower()
    return hashlib.sha256("\n".join([normalized, *addresses]).encode("utf-8")).hexdigest()


def graph_recipient_addresses(message: dict) -> list[str]:
    """The To and Cc addresses of a Graph message resource (Bcc is not in a copy received)."""
    addresses: list[str] = []
    for field in ("toRecipients", "ccRecipients"):
        for recipient in message.get(field) or []:
            if isinstance(recipient, dict):
                address = (recipient.get("emailAddress") or {}).get("address")
                if isinstance(address, str):
                    addresses.append(address)
    return addresses


def _mail_send_key(integration_id: object, assignment_id: object, fingerprint: str) -> str:
    return agent_post_key("microsoft_mail_sent", str(integration_id), str(assignment_id), fingerprint)


# ---------------------------------------------------------------------------
# Microsoft To Do: a write is the task id and the lastModifiedDateTime it left
# ---------------------------------------------------------------------------
#
# Recorded for every write through the platform's To Do tools, in or out of an
# assignment run: the ``todo_task_changed`` trigger always skips its own
# assignment's writes (loop protection) and, with ``exclude_self``, every write
# the platform made. A delete is recorded with the stamp ``deleted``.

_TODO_KEY_PREFIX = "agent_todo_write"
#: Who wrote outside an assignment run (a chat, another tool path).
TODO_WRITER_PLATFORM = "platform"


def _todo_key(task_id: str) -> str:
    return f"{_TODO_KEY_PREFIX}:{task_id}"


async def record_todo_write(task: dict) -> None:
    """Remember a To Do task write the platform made (create, update, complete, delete). Never raises."""
    if not isinstance(task, dict):
        return
    task_id = str(task.get("id") or "")
    stamp = str(task.get("lastModifiedDateTime") or "")
    if not task_id or not stamp:
        return
    try:
        redis = await redis_client()
        key = _todo_key(task_id)
        await redis.hset(key, stamp, posting_assignment() or TODO_WRITER_PLATFORM)
        await redis.expire(key, AGENT_POST_TTL_SECONDS)
    except Exception as exc:
        logger.warning(
            "Could not record a To Do task write (a trigger may run on it): %s", format_exception_message(exc)
        )


async def todo_writer(redis, task_id: object, stamp: object) -> str | None:
    """The assignment id (or ``platform``) that wrote this version of a task, ``None`` when no platform write did."""
    if redis is None or not task_id or not stamp:
        return None
    try:
        writer = await redis.hget(_todo_key(str(task_id)), str(stamp))
    except Exception as exc:
        logger.debug("To Do write lookup failed: %s", format_exception_message(exc))
        return None
    if isinstance(writer, bytes):
        writer = writer.decode("utf-8", "replace")
    return writer or None


async def is_own_todo_write(
    redis, task_id: object, stamp: object, assignment_id: object, *, exclude_self: bool = False
) -> bool:
    """Whether a task change must not trigger *assignment_id*: its own run wrote it, or
    (*exclude_self*) any platform write did. ``False`` when it cannot be told."""
    writer = await todo_writer(redis, task_id, stamp)
    if writer is None:
        return False
    return exclude_self or writer == str(assignment_id)


async def record_mail_draft(draft_id: object) -> None:
    """Record a draft the running assignment created (``POST /me/messages``, ``createReply``)."""
    await record_agent_post("microsoft_mail_draft", draft_id)


async def record_mail_send(
    integration_id: object, subject: object, recipients: list[object], *, now: float | None = None
) -> None:
    """Record a mail the running assignment sent through the integration's mailbox. Never raises."""
    assignment_id = posting_assignment()
    if not assignment_id or not integration_id or not recipients:
        return
    key = _mail_send_key(integration_id, assignment_id, mail_fingerprint(subject, recipients))
    sent_at = time.time() if now is None else now
    try:
        redis = await redis_client()
        await redis.zadd(key, {f"{sent_at:.3f}": sent_at})
        await redis.zremrangebyscore(key, "-inf", sent_at - AGENT_POST_TTL_SECONDS)
        await redis.expire(key, AGENT_POST_TTL_SECONDS)
    except Exception as exc:
        logger.warning(
            "Could not record an agent-sent mail (its assignment may run on it): %s",
            format_exception_message(exc),
        )


def _sent_epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


async def is_agent_mail(redis, integration_id: object, message: dict, assignment_id: object) -> bool:
    """The lookup of the Microsoft ``new_email`` poller. ``False`` when it cannot be told.

    A draft *assignment_id*'s run created, by its id; or a mail it sent through this
    mailbox, by its subject and To / Cc addresses and a ``sentDateTime`` within
    ``MAIL_SEND_MATCH_SECONDS`` of the recorded send.
    """
    if redis is None or not assignment_id or not isinstance(message, dict):
        return False
    if await is_agent_post(redis, "microsoft_mail_draft", message.get("id"), assignment_id=assignment_id):
        return True
    sent_at = _sent_epoch(message.get("sentDateTime"))
    recipients = graph_recipient_addresses(message)
    if not integration_id or sent_at is None or not recipients:
        return False
    key = _mail_send_key(integration_id, assignment_id, mail_fingerprint(message.get("subject"), recipients))
    try:
        near = await redis.zcount(key, sent_at - MAIL_SEND_MATCH_SECONDS, sent_at + MAIL_SEND_MATCH_SECONDS)
    except Exception as exc:
        logger.debug("Agent mail lookup failed: %s", format_exception_message(exc))
        return False
    return bool(near)
