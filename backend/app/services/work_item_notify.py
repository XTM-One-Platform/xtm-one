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

"""The notification a work item asks for at its end, delivered by the platform.

A work item may carry ``payload.notify``::

    {"channel": "teams_dm", "user": "<email>", "integration": "<id or name>",
     "message": "<optional text>", "include_result": false}

The recipient is one of: ``user``, a person's email (a 1:1 chat is
opened with them); ``chat_id``, an existing Teams 1:1 or group chat
(``48:notes`` is the sending account's own chat); or ``channel:
"teams_channel"`` with ``team_id`` and ``channel_id``, a Teams channel.

When the item ends - ``done`` or ``cancelled`` by whatever path (an agent, the
REST API, the retention reaper, a cleanup), or ``blocked`` once its pick-up
failed ``work_item_max_dispatch_attempts`` times - the scheduler tick sends
that Teams message itself, through the named Microsoft integration, with the
item's title, its final status and its result (the cause, for a cancellation
or a block). No agent run is involved, so a run cut by a rollout, a pick-up
that never started or a sweep that cancelled the item can no longer swallow
the notification.

Delivery is durable and at most once:

* the state lives on the item, ``payload.notify.delivery`` (``sent_at``,
  ``attempts``, ``last_error``, ``next_attempt_at``, ``gave_up_at``), written
  by this module alone - a generic write keeps the stored state
  (:func:`normalize_notify_payload`);
* each tick takes the undelivered items under ``FOR UPDATE SKIP LOCKED``, so
  two scheduler processes never send the same message;
* a failed send is retried with a doubling backoff, at most
  :data:`MAX_ATTEMPTS` times; after the last one the item's user gets a web
  notification saying the Teams message could not be sent. A refusal no
  retry can fix (Graph's bad request, not found or validation error) ends
  the delivery at its first attempt;
* a ``user`` that is the sending account itself (Graph refuses a 1:1 chat
  with the same member twice) gets the message in the account's own chat,
  ``48:notes``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.core.config import settings
from app.core.utils import format_exception_message, safe_uuid
from app.models.work_item import WorkItem

logger = logging.getLogger(__name__)

NOTIFY_KEY = "notify"
DELIVERY_KEY = "delivery"
#: The channels a notify ask may name. ``teams`` is accepted as an alias of
#: ``teams_dm`` (a person or a chat); ``teams_channel`` is a Teams channel;
#: ``email`` is an Outlook email to a person's address (#842, S9b);
#: ``google_chat`` is a Chat space or a person's DM with the Chat app, the
#: Google integration posting as a user where the app is not (#842, S11).
CHANNELS: frozenset[str] = frozenset({"teams_dm", "teams", "teams_channel", "email", "google_chat"})
GOOGLE_CHAT = "google_chat"
#: The sending account's own Teams chat (Graph's self-chat): nobody else is in it.
SELF_CHAT_ID = "48:notes"
#: Sends tried before the item's user is told on the web instead.
MAX_ATTEMPTS = 5
#: The first retry waits this long; each next one twice as long.
_FIRST_BACKOFF = timedelta(minutes=1)
#: An item that ended longer ago than this is never notified: a notify ask on
#: an item restored from a backup, or one whose sends all predate a fix, does
#: not wake its reader days later.
_WINDOW = timedelta(days=2)
_BATCH = 20
_RESULT_CHARS = 3000
#: Statuses at which a notify ask is delivered (``blocked`` only once its
#: pick-up is exhausted, see :func:`ending`).
_ENDING_STATUSES: tuple[str, ...] = ("done", "cancelled", "blocked")

#: ``(item, notify spec, text) -> None`` - raises on failure.
Sender = Callable[[Any, Mapping[str, Any], str], Awaitable[None]]
#: The integration tool that opens a 1:1 chat and posts its first message.
_CHAT_TOOL = "create_microsoft_chat"
#: The integration tools that post in an existing chat and in a channel.
_CHAT_MESSAGE_TOOL = "send_microsoft_chat_message"
_CHANNEL_TOOL = "send_microsoft_channel_message"
#: The integration tool that sends an email (channel ``email``).
_EMAIL_TOOL = "send_microsoft_email"
#: The sentence each tool answers on success; anything else is a failure.
_SENT: dict[str, str] = {
    _CHAT_TOOL: "Initial message sent.",
    _EMAIL_TOOL: "Email sent to ",
    _CHAT_MESSAGE_TOOL: "Chat message sent.",
    _CHANNEL_TOOL: "Message sent to channel.",
    "update_microsoft_chat_message": "Chat message updated.",
    "update_microsoft_channel_message": "Channel message updated.",
    "send_google_chat_message": "Message sent to ",
}
#: The Google integration tool that posts as a user (channel ``google_chat`` without the app).
_GOOGLE_CHAT_TOOL = "send_google_chat_message"
#: Graph's answer to a 1:1 chat whose recipient is the sending account itself.
_SELF_RECIPIENT = "duplicate chat members"
#: Refusals a retry cannot fix: the integration layer's wording of an HTTP
#: 400, 404 or 422, and a target id the tool refuses before any call.
_PERMANENT = ("bad request", "resource not found", "validation error", "is missing or invalid")


class NotifyError(ValueError):
    """A notify ask that cannot be stored or sent; the message says why."""


class PermanentNotifyError(NotifyError):
    """A send refused for a reason no retry fixes: the delivery ends at once."""


def is_permanent_refusal(text: str) -> bool:
    """Whether a tool's error is one no retry can fix (HTTP 400, 404, 422, a bad id)."""
    low = (text or "").lower()
    return any(marker in low for marker in _PERMANENT)


def _is_teams_id(value: str) -> bool:
    """A Teams id that stays one Graph URL path segment."""
    return (
        bool(value)
        and len(value) <= 512
        and value not in (".", "..")
        and not any(c in value for c in "/\\?#%")
        and not any(c.isspace() for c in value)
    )


def target_label(spec: Mapping[str, Any]) -> str:
    """Who or where a notify ask sends to, for logs and the give-up notice."""
    if spec.get("channel") == "teams_channel":
        return "a Teams channel"
    if spec.get("channel") == "email":
        return f"the email address {spec.get('user') or ''}"
    if spec.get("channel") == GOOGLE_CHAT:
        return (
            f"the Google Chat space {spec['space']}"
            if spec.get("space")
            else f"{spec.get('user') or ''} on Google Chat"
        )
    if spec.get("chat_id"):
        return "the account's own Teams chat" if spec["chat_id"] == SELF_CHAT_ID else "a Teams chat"
    return str(spec.get("user") or "")


def _target_key(spec: Mapping[str, Any]) -> str:
    """The target as ``WORK_ITEM_NOTIFY_TEST_RECIPIENTS`` lists it, lower case."""
    if spec.get("channel") == "teams_channel":
        return f"{spec.get('team_id') or ''}/{spec.get('channel_id') or ''}".strip().lower()
    if spec.get("channel") == GOOGLE_CHAT and spec.get("space"):
        return str(spec["space"]).strip().lower()
    if spec.get("chat_id"):
        return str(spec["chat_id"]).strip().lower()
    return str(spec.get("user") or "").strip().lower()


def _clean_target(spec: Mapping[str, Any], channel: str) -> dict[str, str]:
    """The recipient fields of a notify ask, checked."""
    if channel == "teams_channel":
        team_id = str(spec.get("team_id") or "").strip()
        channel_id = str(spec.get("channel_id") or "").strip()
        if not (_is_teams_id(team_id) and _is_teams_id(channel_id)):
            raise NotifyError(
                "payload.notify with channel teams_channel needs team_id and channel_id "
                "(list_microsoft_teams_and_channels gives them)"
            )
        return {"channel": "teams_channel", "team_id": team_id, "channel_id": channel_id}
    user = str(spec.get("user") or "").strip()
    if channel == GOOGLE_CHAT:
        return _clean_google_chat_target(spec, user)
    if channel == "email":
        if "@" not in user or any(c.isspace() or c == "," for c in user):
            raise NotifyError("payload.notify with channel email needs user, one email address")
        return {"channel": "email", "user": user}
    chat_id = str(spec.get("chat_id") or "").strip()
    if user and chat_id:
        raise NotifyError(
            "payload.notify names either user (a person's email) or chat_id "
            "(an existing Teams chat), not both"
        )
    if chat_id:
        if not _is_teams_id(chat_id):
            raise NotifyError(
                "payload.notify.chat_id is not a Teams chat id (list_microsoft_chats gives "
                f"them; {SELF_CHAT_ID} is the sending account's own chat)"
            )
        return {"channel": "teams_dm", "chat_id": chat_id}
    if "@" not in user:
        raise NotifyError(
            "payload.notify.user must be the recipient's email address; for a chat or a "
            "channel that is not a person, set chat_id, or channel teams_channel with "
            "team_id and channel_id"
        )
    return {"channel": "teams_dm", "user": user}


def _clean_google_chat_target(spec: Mapping[str, Any], user: str) -> dict[str, str]:
    """A Google Chat target: ``space`` (``spaces/<id>``) or ``user`` (a person's email, the app's DM)."""
    from app.services.google_chat_decision_cards import space_name

    raw_space = str(spec.get("space") or "").strip()
    if raw_space and user:
        raise NotifyError("payload.notify with channel google_chat names either space or user, not both")
    if raw_space:
        space = space_name(raw_space)
        if space is None:
            raise NotifyError("payload.notify.space is not a Google Chat space name (spaces/ followed by the space id)")
        return {"channel": GOOGLE_CHAT, "space": space}
    if "@" not in user or any(c.isspace() or c == "," for c in user):
        raise NotifyError(
            "payload.notify with channel google_chat needs space (spaces/ followed by the space id) or user, one email address"
        )
    return {"channel": GOOGLE_CHAT, "user": user}


def notify_spec(payload: Any) -> dict[str, Any] | None:
    """The notify ask of a payload, or ``None`` when it carries none."""
    if not isinstance(payload, Mapping):
        return None
    spec = payload.get(NOTIFY_KEY)
    return dict(spec) if isinstance(spec, Mapping) else None


#: Correlation key prefixes and title prefixes that mark a test item.
_TEST_KEY_PREFIXES: tuple[str, ...] = ("retest:", "test:")
_TEST_TITLE_PREFIXES: tuple[str, ...] = ("notify retest",)


def _test_recipients() -> frozenset[str]:
    raw = str(getattr(settings, "work_item_notify_test_recipients", "") or "")
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def is_test_item(correlation_key: str | None, title: str | None) -> bool:
    """Whether a work item is a test, by its correlation key or its title."""
    key = (correlation_key or "").strip().lower()
    name = (title or "").strip().lower()
    return key.startswith(_TEST_KEY_PREFIXES) or name.startswith(_TEST_TITLE_PREFIXES)


def refuse_test_recipient(payload: Any, *, correlation_key: str | None, title: str | None) -> None:
    """Refuse a notify ask of a test item that names a recipient outside the test list.

    A test item (key ``retest:`` or ``test:``, title ``Notify retest``) may only
    message a target of ``WORK_ITEM_NOTIFY_TEST_RECIPIENTS`` (an address, a chat
    id or ``team_id/channel_id``), so a platform retest never reaches a real
    person. The sending account's own chat, ``48:notes``, is always
    allowed: nobody else is in it.
    """
    spec = notify_spec(payload)
    if spec is None or not is_test_item(correlation_key, title):
        return
    target = _target_key(spec)
    if target == SELF_CHAT_ID or target in _test_recipients():
        return
    raise NotifyError(
        "a test work item (correlation key starting with 'retest:' or 'test:', or a title "
        "starting with 'Notify retest') may only notify a target listed in "
        f"WORK_ITEM_NOTIFY_TEST_RECIPIENTS or the account's own chat (chat_id {SELF_CHAT_ID}); "
        f"'{target}' is not"
    )


def normalize_notify_payload(
    payload: Any,
    previous: Any = None,
    *,
    correlation_key: str | None = None,
    title: str | None = None,
) -> Any:
    """*payload* with its notify ask checked, its delivery state the stored one.

    Raises :class:`NotifyError` for an ask the platform could not send: an
    unknown channel, no recipient (an address, a chat id, or a team and
    channel id), no integration, or a test item naming a recipient outside
    the test list (:func:`refuse_test_recipient`).
    The ``delivery`` state is the platform's: a writer never sets nor clears
    it, so an update that rewrites the payload keeps what was already sent.
    """
    spec = notify_spec(payload)
    if spec is None:
        if isinstance(payload, Mapping) and NOTIFY_KEY in payload:
            raise NotifyError("payload.notify must be an object")
        return payload
    # A team and channel id without a channel name is a channel target.
    default = "teams_channel" if spec.get("team_id") or spec.get("channel_id") else "teams_dm"
    channel = str(spec.get("channel") or default).strip().lower()
    if channel not in CHANNELS:
        raise NotifyError(
            f"payload.notify.channel '{channel}' is not supported "
            "(supported: teams_dm, teams_channel, email, google_chat)"
        )
    target = _clean_target(spec, channel)
    integration = str(spec.get("integration") or "").strip()
    if not integration and channel != GOOGLE_CHAT:
        raise NotifyError(
            "payload.notify.integration must name the Microsoft integration that sends it"
        )
    # Google Chat sends as the Chat app; the integration (a Google account) is
    # the fallback that posts as a user where the app is not.
    cleaned: dict[str, Any] = {**target, **({"integration": integration} if integration else {})}
    message = spec.get("message")
    if isinstance(message, str) and message.strip():
        cleaned["message"] = message.strip()[:1000]
    if spec.get("include_result") is True:
        cleaned["include_result"] = True
    refuse_test_recipient({NOTIFY_KEY: cleaned}, correlation_key=correlation_key, title=title)
    stored = (notify_spec(previous) or {}).get(DELIVERY_KEY)
    if isinstance(stored, Mapping):
        cleaned[DELIVERY_KEY] = dict(stored)
    return {**payload, NOTIFY_KEY: cleaned}


def _max_dispatch_attempts() -> int:
    return max(int(getattr(settings, "work_item_max_dispatch_attempts", 5) or 0), 1)


def ending(item: Any) -> str | None:
    """How *item* ended, for its notification: ``done``, ``cancelled``, ``blocked`` or ``None``.

    ``blocked`` counts only when the platform parked it after its pick-up
    failed every dispatch attempt (``release_work_item_claim``): an item an
    agent blocked on purpose, or one waiting for other items, has not ended.
    """
    status = str(getattr(item, "status", "") or "")
    if status in ("done", "cancelled"):
        return status
    if status == "blocked" and not getattr(item, "blocked_on", None):
        payload = getattr(item, "payload", None) or {}
        try:
            attempts = int(payload.get("_dispatch_attempts", 0))
        except (TypeError, ValueError):
            attempts = 0
        if attempts >= _max_dispatch_attempts():
            return "blocked"
    return None


def due_for_delivery(item: Any, now: datetime) -> dict[str, Any] | None:
    """The notify ask to send for *item* now, or ``None``."""
    spec = notify_spec(getattr(item, "payload", None))
    if spec is None or ending(item) is None:
        return None
    delivery = spec.get(DELIVERY_KEY) or {}
    if delivery.get("sent_at") or delivery.get("gave_up_at"):
        return None
    next_at = _parse_time(delivery.get("next_attempt_at"))
    if next_at is not None and next_at > now:
        return None
    return spec


def notification_text(item: Any, spec: Mapping[str, Any]) -> str:
    """The message a person reads, in plain words.

    The ask's ``message`` verbatim when it has one; otherwise one sentence
    built from the item's title and how it ended. The item's result (the
    cause, for a cancellation) is added only when ``include_result`` is set.
    Never a correlation key, an item id or a timestamp.
    """
    how = ending(item) or str(getattr(item, "status", "") or "")
    result = (getattr(item, "result", None) or "").strip()
    if len(result) > _RESULT_CHARS:
        result = result[: _RESULT_CHARS - 1] + "\u2026"
    include = spec.get("include_result") is True and bool(result)
    message = str(spec.get("message") or "").strip()
    if message:
        return f"{message}\n\n{result}" if include else message
    title = (str(getattr(item, "title", "") or "").strip().rstrip(".")) or "A work item"
    if how == "done":
        sentence = f"{title} is done."
        return f"{sentence}\n\n{result}" if include else sentence
    if how == "cancelled":
        if include:
            return f"{title} was cancelled: {result.rstrip('.')}."
        return f"{title} was cancelled."
    if how == "blocked":
        sentence = f"{title} could not be started."
        return f"{sentence}\n\n{result}" if include else sentence
    return f"{title} has ended."


def record_attempt(
    spec: Mapping[str, Any], now: datetime, error: str | None, *, permanent: bool = False
) -> dict[str, Any]:
    """*spec* with one more delivery attempt recorded (sent when *error* is ``None``).

    A *permanent* failure (a refusal no retry fixes) gives up at once.
    """
    updated = dict(spec)
    delivery = dict(updated.get(DELIVERY_KEY) or {})
    attempts = int(delivery.get("attempts", 0) or 0) + 1
    delivery["attempts"] = attempts
    delivery["last_attempt_at"] = now.isoformat()
    if error is None:
        delivery["sent_at"] = now.isoformat()
        delivery.pop("last_error", None)
        delivery.pop("next_attempt_at", None)
    else:
        delivery["last_error"] = error[:500]
        if attempts >= MAX_ATTEMPTS or permanent:
            delivery["gave_up_at"] = now.isoformat()
            delivery.pop("next_attempt_at", None)
        else:
            delivery["next_attempt_at"] = (
                now + _FIRST_BACKOFF * (2 ** (attempts - 1))
            ).isoformat()
    updated[DELIVERY_KEY] = delivery
    return updated


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _resolve_integration(db: Any, item: Any, reference: str, provider: str = "microsoft") -> str:
    """The id of the *provider* integration *reference* names, visible to the item's user."""
    if not item.user_id:
        raise NotifyError("the item belongs to no user, so no integration can send for it")
    from app.api.v1.helpers import integration_visible_filter
    from app.models.integration import Integration

    visible = await integration_visible_filter(db, item.user_id)
    from app.services.builtin_tools.id_prefix import id_prefix, id_prefix_clause

    prefix = id_prefix(reference)
    if prefix is not None:
        # An id prefix of 8+ hex (#2264), the form the other tools take; the name still wins.
        named = (
            (
                await db.execute(
                    select(Integration.id).where(
                        Integration.name == reference, Integration.provider == provider, visible
                    )
                )
            )
            .scalars()
            .first()
        )
        if named is not None:
            return str(named)
        matches = (
            (
                await db.execute(
                    select(Integration.id)
                    .where(id_prefix_clause(Integration.id, prefix), Integration.provider == provider, visible)
                    .order_by(Integration.id)
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        if len(matches) > 1:
            raise NotifyError(f"'{reference}' starts the id of several integrations: pass the full id or the name")
        if matches:
            return str(matches[0])
    clause = (
        Integration.id == safe_uuid(reference)
        if safe_uuid(reference) is not None
        else Integration.name == reference
    )
    found = (
        await db.execute(
            select(Integration.id)
            .where(clause, Integration.provider == provider, visible)
            .order_by(Integration.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if found is None:
        label = "Microsoft" if provider == "microsoft" else provider.capitalize()
        raise NotifyError(f"no {label} integration '{reference}' the item's owner may use")
    return str(found)


async def _usable_agent(db: Any, item: Any, agent_id: Any) -> Any:
    """The agent that sends, checked live as before a scheduled action runs.

    The agent exists and is enabled, the item's user is active and may still
    use the agent (``user_visible_filter``); otherwise nothing is sent.
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.agent import Agent
    from app.models.user import User

    agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    if agent is None or not agent.enabled:
        raise NotifyError("the agent that created the item was deleted or disabled")
    user = (
        (await db.execute(select(User).where(User.id == item.user_id))).scalar_one_or_none()
        if item.user_id
        else None
    )
    if user is None or not user.is_active:
        raise NotifyError("the account the item belongs to is missing or deactivated")
    group_ids = await get_user_group_ids(db, user.id)
    usable = (
        await db.execute(
            select(Agent.id).where(
                Agent.id == agent.id,
                user_visible_filter(Agent, user.id, group_ids, is_admin=user.is_admin),
            )
        )
    ).scalar_one_or_none()
    if usable is None:
        raise NotifyError(f"the item's owner can no longer use the agent {agent.name}")
    return agent


def _tool_call(spec: Mapping[str, Any], text: str, subject: str = "") -> tuple[str, dict[str, Any]]:
    """The integration tool and its arguments that deliver *text* to the ask's target.

    *subject* is the email's subject (channel ``email``); the Teams tools ignore it.
    """
    if spec.get("channel") == "email":
        return _EMAIL_TOOL, {
            "to": str(spec["user"]),
            "subject": (subject or "Work item notification")[:200],
            "body": text,
            "content_type": "text",
        }
    if spec.get("channel") == "teams_channel":
        return _CHANNEL_TOOL, {
            "team_id": str(spec["team_id"]),
            "channel_id": str(spec["channel_id"]),
            "message": text,
        }
    if spec.get("chat_id"):
        return _CHAT_MESSAGE_TOOL, {"chat_id": str(spec["chat_id"]), "message": text}
    return _CHAT_TOOL, {"members": str(spec["user"]), "message": text}


def _failure(content: str) -> NotifyError:
    """The error a failed tool answer raises: permanent when no retry fixes it."""
    message = content[:500] or "the Teams message was not sent"
    return PermanentNotifyError(message) if is_permanent_refusal(content) else NotifyError(message)


#: ``(tool, arguments) -> (sent, answer)``: one Microsoft tool call of an agent.
GraphCall = Callable[[str, dict[str, Any]], Awaitable[tuple[bool, str]]]


@asynccontextmanager
async def agent_graph_caller(item: Any, integration_ref: str, provider: str = "microsoft") -> AsyncIterator[GraphCall]:
    """Microsoft tool calls as the item's agent may make them, through the named integration.

    The agent that created the item (its owner when no agent created it),
    resolved exactly as a scheduled exact action of that agent
    (``resolve_exact_toolset``): its bindings, read-only integrations,
    allowed tools, enforced element scope and deny list, with the named
    integration pinned, and the approval gate's answer, after the live checks
    of :func:`_usable_agent`. An agent that may not send a Teams message
    through that integration cannot make the platform send one for it. Runs
    on its own session (a token refresh commits its session). *item* needs
    ``id``, ``user_id``, ``created_by_agent_id`` and ``owner_agent_id``.
    """
    from app.core.database import async_session
    from app.services.scheduled_work_item_runner import (
        approval_refusal,
        resolve_exact_toolset,
    )
    from app.tools.base import ToolCall

    agent_id = item.created_by_agent_id or item.owner_agent_id
    if agent_id is None:
        raise NotifyError("no agent created or owns the item, so none may send its notification")
    async with async_session() as db:
        agent = await _usable_agent(db, item, agent_id)
        integration_id = await _resolve_integration(db, item, integration_ref, provider)
        toolset = await resolve_exact_toolset(
            db, agent, str(item.user_id), pinned={provider: integration_id}
        )

        async def _call(tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
            # The tool of THAT integration: with two Microsoft accounts bound the
            # tools are suffixed per account, and the pinned one is the sender.
            definition = next(
                (
                    d
                    for name in toolset.names
                    if (d := toolset.composite.get_tool_definition(name)) is not None
                    and (d.base_name or d.name) == tool
                    and str(d.entity_id or "") == integration_id
                ),
                None,
            )
            if definition is None:
                raise NotifyError(
                    f"{agent.name} may not send a Teams message through that integration "
                    f"with {tool} (not bound, read-only, out of its scope or denied)"
                )
            call = ToolCall(
                id=f"wi-notify-{str(item.id)[:8]}",
                name=definition.name,
                arguments=arguments,
            )
            refusal = await approval_refusal(db, agent, str(item.user_id), toolset, call)
            if refusal:
                raise NotifyError(refusal)
            result = await toolset.composite.execute_tool(call)
            await db.commit()
            content = result.content if isinstance(result.content, str) else str(result.content or "")
            # A tool may report a failed message inside a successful answer;
            # only its own success sentence counts as sent.
            return (not result.is_error and _SENT.get(tool, "\0") in content), content

        yield _call


async def _send_teams_dm(item: Any, spec: Mapping[str, Any], text: str) -> None:
    """Send *text* to the ask's target (a person, a chat or a channel), as the item's agent may.

    The send is a tool call of :func:`agent_graph_caller`. A ``user`` that is
    the sending account itself is refused by Graph ("Duplicate chat
    members"); the text then goes to the account's own chat (``48:notes``)
    through ``send_microsoft_chat_message``. A refusal no retry fixes raises
    :class:`PermanentNotifyError`.

    An item that waits for a decision goes as the bot's card first when the
    bot is in the target (#842 S9, :func:`_send_decision_card`).
    """
    if spec.get("channel") == GOOGLE_CHAT:
        await _send_google_chat(item, spec, text)
        return
    if (item.created_by_agent_id or item.owner_agent_id) is not None and await _send_decision_card(item, spec):
        return
    async with agent_graph_caller(item, str(spec["integration"])) as _call:
        tool, arguments = _tool_call(spec, text, subject=str(getattr(item, "title", "") or ""))
        sent, content = await _call(tool, arguments)
        if not sent and tool == _CHAT_TOOL and _SELF_RECIPIENT in content.lower():
            # The recipient is the sending account: its own chat is the 1:1.
            try:
                sent, content = await _call(
                    _CHAT_MESSAGE_TOOL, {"chat_id": SELF_CHAT_ID, "message": text}
                )
            except NotifyError as exc:
                raise PermanentNotifyError(
                    f"{spec.get('user')} is the sending account itself, and its own chat "
                    f"could not be used ({exc}); set payload.notify.chat_id to a Teams chat "
                    "instead"
                ) from exc
    if not sent:
        raise _failure(content)


async def _send_google_chat(item: Any, spec: Mapping[str, Any], text: str) -> None:
    """Send *text* to a Google Chat space or a person's DM (#842, S11).

    As the Chat app first (the channel bot, app auth); where the app is not
    in the space or has no DM with the person, as a user through the Google
    integration the ask names (a space only: a user-auth DM needs its space
    name). Nothing could send: :class:`PermanentNotifyError`.
    """
    from app.core.database import async_session
    from app.services import google_chat_decision_cards as gchat

    async with async_session() as db:
        space = await gchat.app_space_for(db, spec)
    if space is not None:
        token = await gchat.app_token()
        channel_sent = bool(token) and await gchat.post_message(token, space, {"text": text}) is not None
        if channel_sent:
            return
    integration = str(spec.get("integration") or "")
    if not integration or not spec.get("space"):
        raise PermanentNotifyError(
            f"the Google Chat app could not post to {target_label(spec)}, and no Google integration "
            "and space name are set to post as a user"
        )
    async with agent_graph_caller(item, integration, "google") as _call:
        sent, content = await _call(_GOOGLE_CHAT_TOOL, {"space_name": str(spec["space"]), "message": text})
    if not sent:
        raise _failure(content)


async def _bot_target(spec: Mapping[str, Any]) -> dict[str, str] | None:
    """The Teams bot's conversation for the ask's target, or ``None`` when the bot is not there."""
    from app.services.decision_card_delivery import bot_conversation, personal_bot_conversation

    if spec.get("channel") == "teams_channel":
        return await bot_conversation(str(spec.get("channel_id") or ""))
    if spec.get("chat_id"):
        if spec["chat_id"] == SELF_CHAT_ID:
            return None
        return await bot_conversation(str(spec["chat_id"]))
    return await personal_bot_conversation(str(spec.get("user") or ""))


async def _send_decision_card(item: Any, spec: Mapping[str, Any]) -> bool:
    """Send the item's open decision as the bot's card when the bot is in the target (#842, S9, I.2).

    ``False`` (the text goes through the agent's account as before) when the
    item waits for no decision, the bot is not in the target conversation, or
    Teams did not take the card.
    """
    if not getattr(item, "needs_human", False):
        return False
    from app.core.database import async_session
    from app.services.channel_notices import notice_language
    from app.services.decision_card_delivery import open_decision, send_decision_card

    try:
        conversation = await _bot_target(spec)
        if conversation is None:
            return False
        async with async_session() as db:
            decision = await open_decision(db, "work_item", item.id)
            if decision is None:
                return False
            return await send_decision_card(db, decision, conversation, viewer=None, language=notice_language())
    except Exception as exc:
        logger.warning("Work item %s: decision card not sent: %s", item.id, format_exception_message(exc))
        return False


async def _give_up_on_web(db: Any, item: Any, spec: Mapping[str, Any], error: str) -> None:
    """Tell the item's user on the web that the Teams message could not be sent."""
    if not item.user_id:
        return
    from app.services.notify import notify_web_user

    await notify_web_user(
        db,
        str(item.user_id),
        title=f"Teams notification not sent: {item.title}"[:500],
        summary=f"The Teams message to {target_label(spec)} could not be sent: {error}",
        source_label="Work items",
        extra_data={"work_item_id": str(item.id)},
    )


async def deliver_pending_notifications(
    *, limit: int = _BATCH, send: Sender | None = None
) -> int:
    """Send every notify ask of an ended work item that is due; returns the number sent.

    Called by the scheduler tick (step 3b). Each item is taken under
    ``FOR UPDATE SKIP LOCKED`` and its delivery state committed with it; the
    send runs on a session of its own, so the lock is held until the state
    is written and a second scheduler process skips the item. A rollout
    between the send and the commit costs at most one repeat, never a
    silence. *send* replaces the Teams delivery in tests.
    """
    from app.core.database import async_session

    now = datetime.now(UTC)
    sent = 0
    async with async_session() as db:
        candidates = (
            await db.execute(
                select(WorkItem)
                .where(
                    WorkItem.status.in_(_ENDING_STATUSES),
                    WorkItem.updated_at >= now - _WINDOW,
                    func.json_typeof(WorkItem.payload[NOTIFY_KEY]) == "object",
                    WorkItem.payload[NOTIFY_KEY][DELIVERY_KEY]["sent_at"].as_string().is_(None),
                    WorkItem.payload[NOTIFY_KEY][DELIVERY_KEY]["gave_up_at"].as_string().is_(None),
                )
                .order_by(WorkItem.updated_at.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        for item in candidates:
            spec = due_for_delivery(item, now)
            if spec is None:
                continue
            error: str | None = None
            permanent = False
            try:
                # On its own session (``_send_teams_dm``): nothing the send
                # does commits or releases the lock this session holds.
                await (send or _send_teams_dm)(item, spec, notification_text(item, spec))
            except Exception as exc:
                error = format_exception_message(exc)
                permanent = isinstance(exc, PermanentNotifyError)
            updated = record_attempt(spec, now, error, permanent=permanent)
            # A new dict, so the JSON column sees the change.
            item.payload = {**(item.payload or {}), NOTIFY_KEY: updated}
            if error is None:
                sent += 1
                logger.info("Work item %s: notification sent to %s", item.id, target_label(spec))
            else:
                logger.warning(
                    "Work item %s: notification to %s failed (attempt %s): %s",
                    item.id,
                    target_label(spec),
                    updated[DELIVERY_KEY]["attempts"],
                    error,
                )
                if updated[DELIVERY_KEY].get("gave_up_at"):
                    await _give_up_on_web(db, item, spec, error)
        await db.commit()
    return sent


def notify_payload_for(
    *,
    integration: str | uuid.UUID,
    user: str | None = None,
    chat_id: str | None = None,
    team_id: str | None = None,
    channel_id: str | None = None,
    message: str | None = None,
    include_result: bool = False,
    email: bool = False,
) -> dict[str, Any]:
    """A ``payload.notify`` value, for callers that build one in code.

    The target is *user* (a person), *chat_id* (a Teams chat) or *team_id*
    and *channel_id* (a Teams channel); *email* sends to *user* by email.
    """
    spec: dict[str, Any]
    if email:
        spec = {"channel": "email", "user": user or ""}
    elif team_id or channel_id:
        spec = {"channel": "teams_channel", "team_id": team_id or "", "channel_id": channel_id or ""}
    elif chat_id:
        spec = {"channel": "teams_dm", "chat_id": chat_id}
    else:
        spec = {"channel": "teams_dm", "user": user or ""}
    spec["integration"] = str(integration)
    if message:
        spec["message"] = message
    if include_result:
        spec["include_result"] = True
    return spec
